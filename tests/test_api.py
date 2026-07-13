import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    TestClient = None

from app.main import create_app
from app.models import Classification


class FakePicker:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def choose(self) -> Path:
        return self.folder


class FakeClassifier:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes) or [
            Classification(
                status="matched",
                predicted_number=1,
                confidence=0.95,
                reason="visible red item",
                matched_features=("red",),
            )
        ]
        self.calls = []

    async def classify(self, encoded, rules):
        self.calls.append((encoded, rules))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@unittest.skipIf(TestClient is None, "FastAPI is not installed in the current environment")
class ApiFlowTests(unittest.TestCase):
    def test_injected_fake_classifier_review_plan_commit_undo(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            folder = Path(tmp)
            Image.new("RGB", (10, 10), "red").save(folder / "photo.jpg")
            classifier = FakeClassifier()
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=classifier,
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                selected = client.post("/api/folders/select").json()
                created = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": selected["folder_id"],
                        "rules": [{"number": 1, "description": "red product"}],
                    },
                ).json()
                job_id = created["job_id"]
                job = client.get(f"/api/jobs/{job_id}").json()
                self.assertEqual(job["status"], "completed")
                self.assertEqual(len(classifier.calls), 1)
                plan = client.post(f"/api/jobs/{job_id}/plan").json()
                committed = client.post(
                    f"/api/jobs/{job_id}/commit", json={"plan_id": plan["plan_id"]}
                ).json()
                self.assertEqual(committed["status"], "committed")
                self.assertTrue((folder / "1.jpg").exists())
                undone = client.post(
                    "/api/history/undo",
                    json={
                        "folder_id": selected["folder_id"],
                        "manifest_id": committed["manifest_id"],
                    },
                ).json()
                self.assertEqual(undone["status"], "undone")
                self.assertTrue((folder / "photo.jpg").exists())

    def test_unknown_thumbnail_item_returns_404(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            folder = Path(tmp)
            Image.new("RGB", (10, 10), "red").save(folder / "photo.jpg")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                response = client.get(f"/api/jobs/{job_id}/items/unknown/thumbnail")
                self.assertEqual(response.status_code, 404)

    def test_from_upload_creates_client_folder_session_and_job(self):
        from io import BytesIO

        from PIL import Image

        with tempfile.TemporaryDirectory() as history:
            buffer = BytesIO()
            Image.new("RGB", (12, 12), "blue").save(buffer, format="JPEG")
            payload = buffer.getvalue()
            app = create_app(
                folder_picker=FakePicker(Path(history)),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                selected = client.post(
                    "/api/folders/from-upload",
                    data={"folder_name": "win-desktop"},
                    files=[("files", ("photo.jpg", payload, "image/jpeg"))],
                )
                self.assertEqual(selected.status_code, 200)
                body = selected.json()
                self.assertEqual(body["mode"], "client")
                self.assertEqual(body["folder_name"], "win-desktop")
                self.assertEqual(body["image_count"], 1)
                created = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": body["folder_id"],
                        "rules": [{"number": 1, "description": "blue product"}],
                    },
                )
                self.assertEqual(created.status_code, 200)
                job = client.get(f"/api/jobs/{created.json()['job_id']}").json()
                self.assertEqual(job["status"], "completed")
                self.assertEqual(job["items"][0]["original_name"], "photo.jpg")

    def test_thumbnail_rejects_parent_replaced_by_symlink_without_leaking_target(self):
        from PIL import Image

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside,
            tempfile.TemporaryDirectory() as history,
        ):
            parent = Path(tmp)
            folder = parent / "images"
            folder.mkdir()
            source = folder / "photo.jpg"
            Image.new("RGB", (10, 10), "red").save(source)
            external = Path(outside)
            secret = external / "photo.jpg"
            secret.write_bytes(b"do-not-leak")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                item_id = client.get(f"/api/jobs/{job_id}").json()["items"][0]["id"]
                folder.rename(parent / "original-images")
                folder.symlink_to(external, target_is_directory=True)

                response = client.get(f"/api/jobs/{job_id}/items/{item_id}/thumbnail")

                self.assertEqual(response.status_code, 400)
                self.assertNotIn(secret.read_bytes(), response.content)

    def test_thumbnail_rejects_file_replaced_by_directory_with_controlled_error(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            folder = Path(tmp)
            source = folder / "photo.jpg"
            Image.new("RGB", (10, 10), "red").save(source)
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                item_id = client.get(f"/api/jobs/{job_id}").json()["items"][0]["id"]
                source.unlink()
                source.mkdir()

                response = client.get(f"/api/jobs/{job_id}/items/{item_id}/thumbnail")

                self.assertEqual(response.status_code, 400)

    def test_thumbnail_rejects_file_replaced_by_symlink_without_leaking_target(self):
        from PIL import Image

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside,
            tempfile.TemporaryDirectory() as history,
        ):
            folder = Path(tmp)
            source = folder / "photo.jpg"
            Image.new("RGB", (10, 10), "red").save(source)
            secret = Path(outside) / "secret.jpg"
            secret.write_bytes(b"do-not-leak")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                item_id = client.get(f"/api/jobs/{job_id}").json()["items"][0]["id"]
                source.unlink()
                source.symlink_to(secret)

                response = client.get(f"/api/jobs/{job_id}/items/{item_id}/thumbnail")

                self.assertEqual(response.status_code, 400)
                self.assertNotIn(secret.read_bytes(), response.content)

    def test_needs_review_must_be_manually_numbered_before_plan(self):
        from PIL import Image

        needs_review = Classification(
            status="needs_review",
            predicted_number=None,
            confidence=0.2,
            reason="ambiguous",
        )
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            folder = Path(tmp)
            Image.new("RGB", (10, 10), "red").save(folder / "photo.jpg")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(needs_review),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                item = client.get(f"/api/jobs/{job_id}").json()["items"][0]
                self.assertIsNone(item["final_number"])
                self.assertFalse(item["explicitly_reviewed"])
                self.assertEqual(client.post(f"/api/jobs/{job_id}/plan").status_code, 409)
                reviewed = client.patch(
                    f"/api/jobs/{job_id}/items/{item['id']}", json={"final_number": 1}
                )
                self.assertEqual(reviewed.status_code, 200)
                plan = client.post(f"/api/jobs/{job_id}/plan")
                self.assertEqual(plan.status_code, 200)
                committed = client.post(
                    f"/api/jobs/{job_id}/commit",
                    json={"plan_id": plan.json()["plan_id"]},
                )
                self.assertEqual(committed.status_code, 200)
                manifest = client.get("/api/history/latest", params={"folder_id": folder_id}).json()
                self.assertEqual(manifest["rules"], [{"number": 1, "description": "red"}])
                entry = next(
                    entry for entry in manifest["entries"] if entry["item_id"] == item["id"]
                )
                self.assertEqual(entry["classification"]["status"], "needs_review")
                self.assertEqual(entry["classification"]["reason"], "ambiguous")
                self.assertEqual(entry["final_number"], 1)
                self.assertTrue(entry["explicitly_reviewed"])

    def test_manual_confirmation_marks_needs_review_item_as_resolved_for_clients(self):
        from PIL import Image

        needs_review = Classification(
            status="needs_review",
            predicted_number=None,
            confidence=0.2,
            reason="ambiguous",
        )
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            folder = Path(tmp)
            Image.new("RGB", (10, 10), "red").save(folder / "photo.jpg")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(needs_review),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                item = client.get(f"/api/jobs/{job_id}").json()["items"][0]

                client.patch(
                    f"/api/jobs/{job_id}/items/{item['id']}", json={"final_number": 1}
                ).raise_for_status()

                reviewed = client.get(f"/api/jobs/{job_id}").json()["items"][0]
                self.assertFalse(reviewed["needs_review"])
                self.assertEqual(client.post(f"/api/jobs/{job_id}/plan").status_code, 200)

    def test_commit_rejects_scan_folder_replaced_after_plan(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            parent = Path(tmp)
            folder = parent / "images"
            folder.mkdir()
            Image.new("RGB", (10, 10), "red").save(folder / "photo.jpg")
            app = create_app(
                folder_picker=FakePicker(folder),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                job_id = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [{"number": 1, "description": "red"}],
                    },
                ).json()["job_id"]
                plan = client.post(f"/api/jobs/{job_id}/plan").json()
                folder.rename(parent / "scanned-images")
                folder.mkdir()
                replacement = folder / "photo.jpg"
                Image.new("RGB", (10, 10), "red").save(replacement)

                response = client.post(
                    f"/api/jobs/{job_id}/commit", json={"plan_id": plan["plan_id"]}
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn("folder", response.json()["detail"].lower())
                self.assertTrue(replacement.exists())
                self.assertFalse((folder / "1.jpg").exists())

    def test_invalid_rules_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as history:
            app = create_app(
                folder_picker=FakePicker(Path(tmp)),
                classifier=FakeClassifier(),
                history_dir=Path(history),
            )
            with TestClient(app) as client:
                folder_id = client.post("/api/folders/select").json()["folder_id"]
                response = client.post(
                    "/api/jobs",
                    json={
                        "folder_id": folder_id,
                        "rules": [
                            {"number": 1, "description": ""},
                            {"number": 1, "description": "duplicate"},
                        ],
                    },
                )
                self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
