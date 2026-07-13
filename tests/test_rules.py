import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.rule_repository import CorruptRuleLibraryError, RuleRepository


class RuleRepositoryTests(unittest.TestCase):
    def test_draft_and_templates_persist_across_repository_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            repository = RuleRepository(path)
            rules = [{"number": 1, "description": "白底正面图"}]

            draft = repository.save_draft(rules, None)
            template = repository.create_template("鞋子主图", rules)

            restored = RuleRepository(path).load()
            self.assertEqual(restored.draft.rules, draft.rules)
            self.assertEqual(restored.draft.template_id, template.id)
            self.assertEqual(restored.last_template_id, template.id)
            self.assertEqual(restored.templates[0].name, "鞋子主图")

    def test_template_names_are_unique_after_unicode_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RuleRepository(Path(tmp) / "rules.json")
            repository.create_template(" Café ", [{"number": 1, "description": "a"}])

            with self.assertRaisesRegex(ValueError, "already exists"):
                repository.create_template("café", [{"number": 2, "description": "b"}])

    def test_update_and_delete_template_preserve_current_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = RuleRepository(Path(tmp) / "rules.json")
            template = repository.create_template("主图", [{"number": 1, "description": "旧规则"}])
            repository.update_template(
                template.id, "详情图", [{"number": 2, "description": "新规则"}]
            )
            before_delete = repository.load().draft

            repository.delete_template(template.id)

            library = repository.load()
            self.assertEqual(library.templates, ())
            self.assertEqual(library.draft.rules, before_delete.rules)
            self.assertIsNone(library.last_template_id)
            self.assertIsNone(library.draft.template_id)

    def test_corrupt_rule_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaises(CorruptRuleLibraryError):
                RuleRepository(path).load()

            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")


class RuleFrontendContractTests(unittest.TestCase):
    def test_setup_page_exposes_template_controls_and_backend_rules_api(self):
        static = Path(__file__).resolve().parents[1] / "app" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")

        for marker in (
            'id="template-select"',
            'id="template-name"',
            'id="save-template"',
            'id="update-template"',
            'id="delete-template"',
            'id="draft-status"',
        ):
            self.assertIn(marker, html)

        self.assertIn("/api/rules", script)
        self.assertIn("/api/rules/draft", script)
        self.assertIn("/api/rules/templates", script)
        self.assertNotIn("localStorage", script)


class RuleApiTests(unittest.TestCase):
    def test_rule_template_crud_and_draft_survive_app_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules_file = Path(tmp) / "rules.json"
            app = create_app(rules_file=rules_file)
            with TestClient(app) as client:
                initial = client.get("/api/rules").json()
                self.assertEqual(initial["templates"], [])
                client.put(
                    "/api/rules/draft",
                    json={"rules": [{"number": 1, "description": "草稿"}]},
                ).raise_for_status()
                created = client.post(
                    "/api/rules/templates",
                    json={
                        "name": "鞋类",
                        "rules": [{"number": 1, "description": "白底鞋图"}],
                    },
                ).json()

            with TestClient(create_app(rules_file=rules_file)) as client:
                restored = client.get("/api/rules").json()
                self.assertEqual(restored["draft"]["rules"][0]["description"], "白底鞋图")
                self.assertEqual(restored["templates"][0]["id"], created["id"])
                updated = client.put(
                    f"/api/rules/templates/{created['id']}",
                    json={
                        "name": "鞋类新版",
                        "rules": [{"number": 2, "description": "场景鞋图"}],
                    },
                ).json()
                self.assertEqual(updated["name"], "鞋类新版")
                client.delete(f"/api/rules/templates/{created['id']}").raise_for_status()
                self.assertEqual(client.get("/api/rules").json()["templates"], [])

    def test_invalid_or_duplicate_template_rules_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(create_app(rules_file=Path(tmp) / "rules.json")) as client:
                response = client.post(
                    "/api/rules/templates",
                    json={
                        "name": "重复编号",
                        "rules": [
                            {"number": 1, "description": "a"},
                            {"number": 1, "description": "b"},
                        ],
                    },
                )
                self.assertEqual(response.status_code, 422)

    def test_corrupt_rule_library_returns_controlled_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text("{broken", encoding="utf-8")
            with TestClient(create_app(rules_file=path), raise_server_exceptions=False) as client:
                response = client.get("/api/rules")
                self.assertEqual(response.status_code, 500)
                self.assertIn("corrupt", response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
