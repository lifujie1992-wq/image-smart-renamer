from __future__ import annotations

import json
import os
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models import Rule, RuleDraft, RuleLibrary, RuleTemplate


class CorruptRuleLibraryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_name(name: str) -> str:
    return unicodedata.normalize("NFC", name.strip()).casefold()


class RuleRepository:
    def __init__(self, path: Path):
        self.path = path.expanduser()

    def load(self) -> RuleLibrary:
        if not self.path.exists():
            return RuleLibrary()
        try:
            return RuleLibrary.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CorruptRuleLibraryError(f"Rule library is corrupt: {self.path.name}") from exc

    def save_draft(self, rules: list[Rule] | list[dict], template_id: str | None) -> RuleDraft:
        library = self.load()
        parsed_rules = tuple(Rule.model_validate(rule) for rule in rules)
        valid_ids = {template.id for template in library.templates}
        selected = template_id if template_id in valid_ids else None
        draft = RuleDraft(rules=parsed_rules, template_id=selected, updated_at=_now())
        self._save(library.model_copy(update={"draft": draft, "last_template_id": selected}))
        return draft

    def create_template(self, name: str, rules: list[Rule] | list[dict]) -> RuleTemplate:
        library = self.load()
        clean_name = self._validated_name(name, library.templates)
        now = _now()
        template = RuleTemplate(
            id=uuid.uuid4().hex,
            name=clean_name,
            rules=tuple(Rule.model_validate(rule) for rule in rules),
            created_at=now,
            updated_at=now,
        )
        updated = library.model_copy(
            update={
                "templates": (*library.templates, template),
                "draft": RuleDraft(rules=template.rules, template_id=template.id, updated_at=now),
                "last_template_id": template.id,
            }
        )
        self._save(updated)
        return template

    def update_template(
        self, template_id: str, name: str, rules: list[Rule] | list[dict]
    ) -> RuleTemplate:
        library = self.load()
        existing = next(
            (template for template in library.templates if template.id == template_id),
            None,
        )
        if existing is None:
            raise KeyError("Unknown rule template")
        clean_name = self._validated_name(name, library.templates, exclude_id=template_id)
        updated_template = existing.model_copy(
            update={
                "name": clean_name,
                "rules": tuple(Rule.model_validate(rule) for rule in rules),
                "updated_at": _now(),
            }
        )
        templates = tuple(
            updated_template if template.id == template_id else template
            for template in library.templates
        )
        draft = RuleDraft(
            rules=updated_template.rules,
            template_id=template_id,
            updated_at=_now(),
        )
        self._save(
            library.model_copy(
                update={
                    "templates": templates,
                    "draft": draft,
                    "last_template_id": template_id,
                }
            )
        )
        return updated_template

    def delete_template(self, template_id: str) -> RuleLibrary:
        library = self.load()
        if not any(template.id == template_id for template in library.templates):
            raise KeyError("Unknown rule template")
        templates = tuple(template for template in library.templates if template.id != template_id)
        draft = library.draft
        if draft.template_id == template_id:
            draft = draft.model_copy(update={"template_id": None, "updated_at": _now()})
        updated = library.model_copy(
            update={
                "templates": templates,
                "draft": draft,
                "last_template_id": (
                    None if library.last_template_id == template_id else library.last_template_id
                ),
            }
        )
        self._save(updated)
        return updated

    @staticmethod
    def _validated_name(
        name: str, templates: tuple[RuleTemplate, ...], exclude_id: str | None = None
    ) -> str:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Template name cannot be empty")
        canonical = _canonical_name(clean_name)
        if any(
            template.id != exclude_id and _canonical_name(template.name) == canonical
            for template in templates
        ):
            raise ValueError("A rule template with this name already exists")
        return clean_name

    def _save(self, library: RuleLibrary) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(library.model_dump(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
