import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import Rule, ScannedImage
from app.services.history_repository import HistoryRepository
from app.services.rename_executor import RenameExecutor, RenameSafetyError
from app.services.rename_planner import build_rename_plan


class RecordingRenameExecutor(RenameExecutor):
    def __init__(self, repository: HistoryRepository) -> None:
        super().__init__(repository)
        self.rename_steps = []

    def _rename_exclusive(self, root: Path, source_name: str, target_name: str) -> None:
        self.rename_steps.append(
            {
                "from": source_name,
                "to": target_name,
                "names_before": frozenset(path.name for path in root.iterdir()),
            }
        )
        super()._rename_exclusive(root, source_name, target_name)


class ExecutorTests(unittest.TestCase):
    def scanned(self, path: Path, item_id: str) -> ScannedImage:
        stat = path.stat()
        return ScannedImage(
            id=item_id,
            original_name=path.name,
            extension=path.suffix.lower(),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            width=1,
            height=1,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def test_real_three_file_exchange_is_strictly_two_phase_and_preserves_manifest_hashes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            paths = [root / "1.jpg", root / "2.jpg", root / "3.jpg"]
            payloads = [b"one", b"two", b"three"]
            for path, payload in zip(paths, payloads, strict=True):
                path.write_bytes(payload)
            items = [
                self.scanned(path, item_id) for path, item_id in zip(paths, "abc", strict=True)
            ]
            plan = build_rename_plan(
                root,
                items,
                {"a": 2, "b": 3, "c": 1},
                {path.name for path in paths},
            )
            executor = RecordingRenameExecutor(HistoryRepository(Path(history)))

            manifest = executor.commit(root, plan, items, rules=[])

            active = [entry for entry in manifest.entries if not entry["no_op"]]
            first_phase = executor.rename_steps[: len(active)]
            second_phase = executor.rename_steps[len(active) :]
            self.assertTrue(
                all(step["to"].startswith(".image-smart-renamer-") for step in first_phase)
            )
            temporary_names = {entry["temporary_name"] for entry in active}
            self.assertTrue(second_phase)
            self.assertTrue(
                temporary_names <= second_phase[0]["names_before"],
                "every source must enter a temporary name before any final name is written",
            )
            self.assertEqual(
                {entry["target_name"]: entry["sha256"] for entry in manifest.entries},
                {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in root.iterdir()
                },
            )
            self.assertEqual((root / "1.jpg").read_bytes(), b"three")
            self.assertEqual((root / "2.jpg").read_bytes(), b"one")
            self.assertEqual((root / "3.jpg").read_bytes(), b"two")

    def test_manifest_persists_rules(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "photo.jpg"
            source.write_bytes(b"photo")
            item = self.scanned(source, "photo")
            plan = build_rename_plan(root, [item], {"photo": 7}, {source.name})

            manifest = RenameExecutor(HistoryRepository(Path(history))).commit(
                root,
                plan,
                [item],
                rules=[Rule(number=7, description="red shoe")],
            )
            stored = HistoryRepository(Path(history)).list_all()[0]

            self.assertEqual(stored.rules, [{"number": 7, "description": "red shoe"}])
            self.assertEqual(stored.model_dump(), manifest.model_dump())

    def test_undo_only_latest_committed_manifest(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            repository = HistoryRepository(Path(history))
            original = root / "a.jpg"
            original.write_bytes(b"a")
            first_item = self.scanned(original, "first")
            first_plan = build_rename_plan(root, [first_item], {"first": 1}, {original.name})
            first_manifest = RenameExecutor(repository).commit(root, first_plan, [first_item], [])

            renamed = root / "1.jpg"
            second_item = self.scanned(renamed, "second")
            second_plan = build_rename_plan(root, [second_item], {"second": 2}, {renamed.name})
            second_manifest = RenameExecutor(repository).commit(
                root, second_plan, [second_item], []
            )

            undone = RenameExecutor(repository).undo_latest(root)

            self.assertEqual(undone.id, second_manifest.id)
            self.assertTrue((root / "1.jpg").exists())
            self.assertFalse((root / "a.jpg").exists())
            by_id = {manifest.id: manifest for manifest in repository.list_all()}
            self.assertEqual(by_id[first_manifest.id].status, "committed")
            self.assertEqual(by_id[second_manifest.id].status, "undone")

    def test_undo_by_manifest_id_uses_one_independent_idempotent_attempt_journal(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            repository = HistoryRepository(Path(history))
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            executor = RenameExecutor(repository)
            committed = executor.commit(root, plan, [item], [])

            first = executor.undo(root, committed.id)
            second = executor.undo(root, committed.id)

            self.assertEqual(second.id, first.id)
            self.assertNotEqual(first.id, committed.id)
            self.assertEqual(first.operation, "undo")
            self.assertEqual(first.manifest_id, committed.id)
            self.assertEqual(first.status, "committed")
            stored = {manifest.id: manifest for manifest in repository.list_all()}
            self.assertEqual(len(stored), 2)
            self.assertEqual(stored[committed.id].status, "committed")
            self.assertEqual(source.read_bytes(), b"a")
            self.assertFalse((root / "1.jpg").exists())

    def test_corrupt_manifest_fails_closed_before_commit(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            history_root = Path(history)
            (history_root / "corrupt.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RenameSafetyError, "corrupt"):
                RenameExecutor(HistoryRepository(history_root)).commit(root, plan, [item], [])

            self.assertEqual(source.read_bytes(), b"a")
            self.assertFalse((root / "1.jpg").exists())

    def test_undo_rejects_externally_modified_target_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            repository = HistoryRepository(Path(history))
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            RenameExecutor(repository).commit(root, plan, [item], [])
            target = root / "1.jpg"
            target.write_bytes(b"external change")

            with self.assertRaises(RenameSafetyError):
                RenameExecutor(repository).undo_latest(root)

            self.assertEqual(target.read_bytes(), b"external change")
            self.assertFalse(source.exists())

    def test_undo_rejects_externally_occupied_original_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            repository = HistoryRepository(Path(history))
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            RenameExecutor(repository).commit(root, plan, [item], [])
            source.write_bytes(b"external occupant")

            with self.assertRaises(RenameSafetyError):
                RenameExecutor(repository).undo_latest(root)

            self.assertEqual(source.read_bytes(), b"external occupant")
            self.assertEqual((root / "1.jpg").read_bytes(), b"a")

    def test_modified_no_op_source_rejects_commit(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "1.jpg"
            source.write_bytes(b"original")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            self.assertTrue(plan.entries[0].no_op)
            source.write_bytes(b"changed")

            with self.assertRaisesRegex(RenameSafetyError, "changed after scanning"):
                RenameExecutor(HistoryRepository(Path(history))).commit(root, plan, [item], [])

            self.assertEqual(source.read_bytes(), b"changed")
            self.assertEqual(HistoryRepository(Path(history)).list_all(), [])

    def test_modified_source_rejects_commit_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {"a.jpg"})
            source.write_bytes(b"changed")
            with self.assertRaises(RenameSafetyError):
                RenameExecutor(HistoryRepository(Path(history))).commit(root, plan, [item], [])
            self.assertEqual(source.read_bytes(), b"changed")
            self.assertFalse((root / "1.jpg").exists())

    def test_commit_never_overwrites_target_created_during_rename(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "a.jpg"
            source.write_bytes(b"source")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            original_link = os.link

            def racing_link(source_path, target_path, **kwargs):
                target = Path(target_path)
                if target.name == "1.jpg":
                    target.write_bytes(b"external")
                return original_link(source_path, target_path, **kwargs)

            with patch("app.services.rename_executor.os.link", side_effect=racing_link):
                with self.assertRaises(RenameSafetyError):
                    RenameExecutor(HistoryRepository(Path(history))).commit(root, plan, [item], [])

            self.assertEqual((root / "1.jpg").read_bytes(), b"external")
            self.assertEqual(source.read_bytes(), b"source")

    def test_crashed_commit_blocks_new_work_until_recovery_rolls_it_back(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "a.jpg"
            source.write_bytes(b"source")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {source.name})
            repository = HistoryRepository(Path(history))
            crashed = RenameExecutor(repository)
            original_rename = crashed._rename_exclusive
            crash_once = True

            def crash_after_rename(root_path, source_name, target_name):
                nonlocal crash_once
                original_rename(root_path, source_name, target_name)
                if crash_once:
                    crash_once = False
                    raise SystemExit("simulated process crash")

            with patch.object(crashed, "_rename_exclusive", side_effect=crash_after_rename):
                with self.assertRaises(SystemExit):
                    crashed.commit(root, plan, [item], [])

            executor = RenameExecutor(repository)
            with self.assertRaisesRegex(RenameSafetyError, "unfinished"):
                executor.commit(root, plan, [item], [])

            recovered = executor.recover_incomplete(root)

            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].status, "rolled_back")
            self.assertEqual(recovered[0].completed_steps, [])
            self.assertEqual(source.read_bytes(), b"source")
            self.assertFalse((root / plan.entries[0].temporary_name).exists())
            committed = executor.commit(root, plan, [item], [])
            self.assertEqual(committed.status, "committed")

    def test_injected_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            root = Path(tmp)
            source = root / "a.jpg"
            source.write_bytes(b"a")
            item = self.scanned(source, "a")
            plan = build_rename_plan(root, [item], {"a": 1}, {"a.jpg"})
            executor = RenameExecutor(HistoryRepository(Path(history)), fail_after_steps=1)
            with self.assertRaises(RuntimeError):
                executor.commit(root, plan, [item], [])
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"a")


if __name__ == "__main__":
    unittest.main()
