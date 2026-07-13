from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Rule(FrozenModel):
    number: int = Field(gt=0)
    description: str = Field(min_length=1)


class ScannedImage(FrozenModel):
    id: str
    original_name: str
    extension: str
    size: int
    mtime_ns: int
    width: int | None
    height: int | None
    sha256: str
    scan_error: str | None = None


class EncodedImage(FrozenModel):
    media_type: Literal["image/jpeg"] = "image/jpeg"
    data: str
    width: int
    height: int


class Classification(FrozenModel):
    status: Literal["matched", "needs_review"]
    predicted_number: int | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str
    matched_features: tuple[str, ...] = ()
    error_code: str | None = None
    request_id: str | None = None


class ReviewItem(BaseModel):
    id: str
    image: ScannedImage
    classification: Classification | None = None
    final_number: int | None = None
    explicitly_reviewed: bool = False
    duplicate_count: int = 1


class RenameEntry(FrozenModel):
    item_id: str
    source_name: str
    target_name: str
    temporary_name: str
    sha256: str
    size: int
    mtime_ns: int
    no_op: bool = False


class RenamePlan(FrozenModel):
    plan_id: str
    review_hash: str
    folder: str
    folder_dev: int | None = None
    folder_ino: int | None = None
    entries: tuple[RenameEntry, ...]


class Manifest(BaseModel):
    version: int = 1
    id: str
    operation: Literal["commit", "undo"] = "commit"
    manifest_id: str | None = None
    folder: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: Literal["prepared", "committed", "rolled_back", "undone", "needs_recovery"]
    plan_id: str
    rules: list[dict]
    entries: list[dict]
    completed_steps: list[dict] = Field(default_factory=list)
    error: str | None = None


class RuleSetRequest(BaseModel):
    rules: list[Rule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_rules(self):
        numbers = [rule.number for rule in self.rules]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Rule numbers must be unique")
        return self


class RuleDraft(FrozenModel):
    rules: tuple[Rule, ...] = ()
    template_id: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class RuleTemplate(FrozenModel):
    id: str
    name: str
    rules: tuple[Rule, ...]
    created_at: str
    updated_at: str


class RuleLibrary(FrozenModel):
    version: int = 1
    draft: RuleDraft = Field(default_factory=RuleDraft)
    templates: tuple[RuleTemplate, ...] = ()
    last_template_id: str | None = None


class RuleDraftRequest(RuleSetRequest):
    template_id: str | None = None


class RuleTemplateRequest(RuleSetRequest):
    name: str = Field(min_length=1)


class JobCreateRequest(RuleSetRequest):
    folder_id: str


class ReviewUpdateRequest(BaseModel):
    final_number: int = Field(gt=0)


class CommitRequest(BaseModel):
    plan_id: str


class UndoRequest(BaseModel):
    folder_id: str
    manifest_id: str
