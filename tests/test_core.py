import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
from PIL import Image
from pydantic import BaseModel

from app.models import Classification, EncodedImage, Rule, ScannedImage
from app.services.claude_classifier import (
    ClaudeClassification,
    ClaudeClassifier,
)
from app.services.duplicate_detector import group_exact_duplicates
from app.services.image_encoder import encode_for_claude
from app.services.job_manager import JobManager
from app.services.rename_planner import PlanConflictError, build_rename_plan
from app.services.scanner import scan_folder


def make_image(path: Path, color=(255, 0, 0), size=(20, 10), mode="RGB") -> None:
    Image.new(mode, size, color).save(path)


def scanned(
    name: str,
    digest: str,
    *,
    item_id: str | None = None,
    size: int = 1,
    mtime_ns: int = 1,
) -> ScannedImage:
    return ScannedImage(
        id=item_id or digest,
        original_name=name,
        extension=Path(name).suffix.lower(),
        size=size,
        mtime_ns=mtime_ns,
        width=1,
        height=1,
        sha256=digest,
    )


class QueueClassifier:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = []

    async def classify(self, encoded, rules):
        self.calls.append((encoded, rules))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ScannerAndEncoderTests(unittest.TestCase):
    def test_current_layer_nfc_casefold_sort_skips_temporary_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["z.PNG", "B.jpg", "é.jpg", "a.JPG"]
            for name in reversed(names):
                make_image(root / name)
            (root / "nested").mkdir()
            make_image(root / "nested" / "hidden.jpg")
            make_image(root / ".image-smart-renamer-stale.jpg")
            (root / "note.txt").write_text("x")
            (root / "link.webp").symlink_to(root / "B.jpg")

            first = scan_folder(root)
            second = scan_folder(root)

            expected_names = ["a.JPG", "B.jpg", "z.PNG", "é.jpg"]
            self.assertEqual([item.original_name for item in first], expected_names)
            self.assertEqual(
                [item.original_name for item in second],
                expected_names,
                "scan ordering must be stable across repeated scans",
            )
            expected_hash = hashlib.sha256((root / "a.JPG").read_bytes()).hexdigest()
            self.assertEqual(first[0].sha256, expected_hash)
            self.assertEqual((first[0].width, first[0].height), (20, 10))

    def test_damaged_image_is_retained_as_blocked_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.jpg"
            bad.write_bytes(b"not an image")
            item = scan_folder(Path(tmp))[0]
            self.assertEqual(item.scan_error, "invalid_image")
            self.assertIsNone(item.width)

    def test_encoder_flattens_alpha_and_does_not_enlarge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alpha.png"
            make_image(path, color=(255, 0, 0, 0), size=(10, 5), mode="RGBA")
            encoded = encode_for_claude(path, max_edge=100)
            self.assertEqual(encoded.width, 10)
            self.assertEqual(encoded.height, 5)
            self.assertTrue(encoded.data)

    def test_duplicate_groups_are_sha_only(self):
        items = [
            scanned("a.jpg", "same", item_id="a"),
            scanned("b.png", "same", item_id="b"),
            scanned("c.jpg", "other"),
        ]
        groups = group_exact_duplicates(items)
        self.assertEqual([[item.id for item in group] for group in groups], [["a", "b"], ["other"]])


class PlannerTests(unittest.TestCase):
    def test_mixed_extensions_are_numbered_deterministically(self):
        items = [scanned("z.jpg", "z"), scanned("a.png", "a"), scanned("m.webp", "m")]
        finals = {item.id: 1 for item in items}
        plan = build_rename_plan(Path("/tmp/pictures"), items, finals, occupied_names=set())
        self.assertEqual(
            [entry.target_name for entry in plan.entries], ["1.png", "1-2.webp", "1-3.jpg"]
        )
        again = build_rename_plan(
            Path("/tmp/pictures"), list(reversed(items)), finals, occupied_names=set()
        )
        self.assertEqual(plan.model_dump(), again.model_dump())

    def test_three_file_exchange_cycle_is_plannable(self):
        items = [
            scanned("1.jpg", "a"),
            scanned("2.jpg", "b"),
            scanned("3.jpg", "c"),
        ]
        plan = build_rename_plan(
            Path("/tmp/x"),
            items,
            {"a": 2, "b": 3, "c": 1},
            occupied_names={"1.jpg", "2.jpg", "3.jpg"},
        )
        self.assertEqual(
            {(entry.source_name, entry.target_name) for entry in plan.entries},
            {("1.jpg", "2.jpg"), ("2.jpg", "3.jpg"), ("3.jpg", "1.jpg")},
        )
        self.assertEqual(len({entry.temporary_name for entry in plan.entries}), 3)

    def test_external_casefold_and_unicode_occupation_conflicts(self):
        from app.services.rename_planner import canonical

        item = scanned("old.jpg", "a")
        with self.assertRaises(PlanConflictError):
            build_rename_plan(Path("/tmp/x"), [item], {"a": 1}, {"1.JPG"})
        composed = "é.jpg"
        decomposed = "é.jpg"
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(canonical(composed), canonical(decomposed))


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    matched = Classification(
        status="matched",
        predicted_number=1,
        confidence=0.9,
        reason="red",
        matched_features=("red",),
    )
    needs_review = Classification(
        status="needs_review",
        predicted_number=None,
        confidence=0.2,
        reason="ambiguous",
    )

    async def test_classifier_called_once_per_sha256_duplicate_group(self):
        classifier = QueueClassifier(self.matched)
        encoder = AsyncMock(return_value=object())
        manager = JobManager(classifier=classifier, encoder=encoder, concurrency=3)
        items = [
            scanned("a.jpg", "same", item_id="a"),
            scanned("b.jpg", "same", item_id="b"),
        ]

        job = await manager.create(Path("/tmp"), items, [Rule(number=1, description="red")])
        await manager.wait(job.id)

        self.assertEqual(len(classifier.calls), 1)
        self.assertEqual(encoder.await_count, 1)
        self.assertTrue(all(item.final_number == 1 for item in job.items))

    async def test_needs_review_and_classifier_exception_require_manual_number(self):
        for result, expected_error in [
            (self.needs_review, None),
            (RuntimeError("offline failure"), "classifier_failure"),
        ]:
            with self.subTest(result=result):
                manager = JobManager(
                    classifier=QueueClassifier(result), encoder=AsyncMock(return_value=object())
                )
                job = await manager.create(
                    Path("/tmp"), [scanned("a.jpg", "a")], [Rule(number=1, description="red")]
                )
                await manager.wait(job.id)
                review = job.items[0]
                self.assertEqual(review.classification.status, "needs_review")
                self.assertIsNone(review.final_number)
                self.assertFalse(review.explicitly_reviewed)
                self.assertEqual(review.classification.error_code, expected_error)

    async def test_retry_clears_duplicate_group_review_and_invalidates_plan_before_call(self):
        observations = []

        class ObservingClassifier:
            async def classify(inner_self, encoded, rules):
                observations.append(
                    (
                        [(item.final_number, item.explicitly_reviewed) for item in job.items],
                        job.plan,
                    )
                )
                return self.needs_review

        manager = JobManager(
            classifier=QueueClassifier(self.matched), encoder=AsyncMock(return_value=object())
        )
        job = await manager.create(
            Path("/tmp"),
            [
                scanned("a.jpg", "same", item_id="a"),
                scanned("b.jpg", "same", item_id="b"),
            ],
            [Rule(number=1, description="red")],
        )
        await manager.wait(job.id)
        for item in job.items:
            item.final_number = 1
            item.explicitly_reviewed = True
        job.plan = object()
        manager.classifier = ObservingClassifier()

        await manager.retry(job.id, job.items[0].id)

        self.assertEqual(observations, [([(None, False), (None, False)], None)])
        self.assertTrue(all(item.final_number is None for item in job.items))
        self.assertTrue(all(not item.explicitly_reviewed for item in job.items))


class ClaudeClassifierTests(unittest.IsolatedAsyncioTestCase):
    encoded = EncodedImage(data="ZmFrZQ==", width=10, height=10)
    rules = [Rule(number=7, description="red shoe")]

    async def test_uses_async_official_parse_with_pydantic_output_and_compatible_signature(self):
        parsed = ClaudeClassification(
            status="matched",
            predicted_number=7,
            confidence=0.95,
            reason="visible red shoe",
            matched_features=["red", "shoe"],
        )
        parse = AsyncMock(
            return_value=SimpleNamespace(
                parsed_output=parsed,
                stop_reason="end_turn",
                _request_id="req_test",
            )
        )
        client = SimpleNamespace(messages=SimpleNamespace(parse=parse))

        result = await ClaudeClassifier(client=client).classify(self.encoded, self.rules)

        parse.assert_awaited_once()
        kwargs = parse.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-4-8")
        self.assertEqual(kwargs["thinking"], {"type": "adaptive"})
        self.assertEqual(kwargs["output_config"], {"effort": "high"})
        self.assertIs(kwargs["output_format"], ClaudeClassification)
        self.assertTrue(issubclass(kwargs["output_format"], BaseModel))
        content = kwargs["messages"][0]["content"]
        self.assertEqual(content[0]["source"]["data"], self.encoded.data)
        self.assertIn("7: red shoe", content[1]["text"])
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.predicted_number, 7)
        self.assertEqual(result.request_id, "req_test")

    async def test_parsed_needs_review_has_no_number(self):
        parse = AsyncMock(
            return_value=SimpleNamespace(
                parsed_output=ClaudeClassification(
                    status="needs_review",
                    predicted_number=7,
                    confidence=0.9,
                    reason="ambiguous",
                    matched_features=[],
                ),
                stop_reason="end_turn",
                _request_id=None,
            )
        )
        result = await ClaudeClassifier(
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse))
        ).classify(self.encoded, self.rules)
        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.predicted_number)

    async def test_parse_validation_error_from_refusal_maps_to_refusal_only(self):
        from anthropic.lib._parse._response import parse_response
        from anthropic.types import Message, TextBlock, Usage
        from pydantic import ValidationError

        refusal = Message(
            id="msg_refusal",
            content=[TextBlock(text="I cannot help with that.", type="text")],
            model="claude-opus-4-8",
            role="assistant",
            stop_reason="refusal",
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=1, output_tokens=1),
        )
        try:
            parse_response(output_format=ClaudeClassification, response=refusal)
        except ValidationError as exc:
            parse = AsyncMock(side_effect=exc)
        else:
            self.fail("Expected refusal text to fail structured parsing")

        result = await ClaudeClassifier(
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse))
        ).classify(self.encoded, self.rules)

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.error_code, "refusal")

    async def test_unrelated_response_local_does_not_map_validation_error_to_refusal(self):
        from anthropic.types import Message, TextBlock, Usage
        from pydantic import TypeAdapter, ValidationError

        response = Message(
            id="msg_unrelated",
            content=[TextBlock(text="refused", type="text")],
            model="claude-opus-4-8",
            role="assistant",
            stop_reason="refusal",
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=1, output_tokens=1),
        )
        try:
            TypeAdapter(ClaudeClassification).validate_json("not json")
        except ValidationError as exc:
            self.assertEqual(response.stop_reason, "refusal")
            parse = AsyncMock(side_effect=exc)
        else:
            self.fail("Expected invalid text to fail structured parsing")

        result = await ClaudeClassifier(
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse))
        ).classify(self.encoded, self.rules)

        self.assertEqual(result.error_code, "invalid_response")

    async def test_other_parse_validation_error_remains_invalid_response(self):
        from anthropic.lib._parse._response import parse_response
        from anthropic.types import Message, TextBlock, Usage
        from pydantic import ValidationError

        invalid = Message(
            id="msg_invalid",
            content=[TextBlock(text="not json", type="text")],
            model="claude-opus-4-8",
            role="assistant",
            stop_reason="end_turn",
            stop_sequence=None,
            type="message",
            usage=Usage(input_tokens=1, output_tokens=1),
        )
        try:
            parse_response(output_format=ClaudeClassification, response=invalid)
        except ValidationError as exc:
            parse = AsyncMock(side_effect=exc)
        else:
            self.fail("Expected invalid text to fail structured parsing")

        result = await ClaudeClassifier(
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse))
        ).classify(self.encoded, self.rules)

        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.error_code, "invalid_response")

    async def test_official_client_api_exception_degrades_to_needs_review_offline(self):
        parse = AsyncMock(side_effect=anthropic.APIConnectionError(request=None))
        result = await ClaudeClassifier(
            client=SimpleNamespace(messages=SimpleNamespace(parse=parse))
        ).classify(self.encoded, self.rules)
        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.predicted_number)
        self.assertEqual(result.error_code, "connection")


if __name__ == "__main__":
    unittest.main()
