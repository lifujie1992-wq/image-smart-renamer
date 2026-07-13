from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from app.models import RenameEntry, RenamePlan, ScannedImage


class PlanConflictError(ValueError):
    pass


def canonical(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _safe_number(value: int | str) -> str:
    text = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]*", text):
        raise ValueError("Final numbers must be positive integers")
    return text


def build_rename_plan(
    folder: Path,
    items: list[ScannedImage],
    final_numbers: dict[str, int | str],
    occupied_names: set[str],
) -> RenamePlan:
    ordered = sorted(items, key=lambda item: (canonical(item.original_name), item.original_name))
    grouped: dict[str, list[ScannedImage]] = {}
    for item in ordered:
        if item.id not in final_numbers:
            raise ValueError(f"Missing final number for {item.original_name}")
        grouped.setdefault(_safe_number(final_numbers[item.id]), []).append(item)
    targets: dict[str, str] = {}
    for number in sorted(grouped, key=lambda value: int(value)):
        for index, item in enumerate(grouped[number], 1):
            suffix = "" if index == 1 else f"-{index}"
            targets[item.id] = f"{number}{suffix}{item.extension}"
    normalized_targets = [canonical(name) for name in targets.values()]
    if len(normalized_targets) != len(set(normalized_targets)):
        raise PlanConflictError("Planned target names conflict")
    source_keys = {canonical(item.original_name) for item in items}
    for target in targets.values():
        if (
            canonical(target) in {canonical(name) for name in occupied_names}
            and canonical(target) not in source_keys
        ):
            raise PlanConflictError(f"Target is occupied outside this plan: {target}")
    review_data = [(item.id, targets[item.id], item.sha256) for item in ordered]
    review_hash = hashlib.sha256(
        json.dumps(review_data, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    entries = tuple(
        RenameEntry(
            item_id=item.id,
            source_name=item.original_name,
            target_name=targets[item.id],
            temporary_name=f".image-smart-renamer-{review_hash[:12]}-{index}",
            sha256=item.sha256,
            size=item.size,
            mtime_ns=item.mtime_ns,
            no_op=item.original_name == targets[item.id],
        )
        for index, item in enumerate(ordered)
    )
    resolved = folder.resolve()
    try:
        folder_stat = resolved.stat()
        folder_identity: tuple[int | None, int | None] = (
            folder_stat.st_dev,
            folder_stat.st_ino,
        )
    except OSError:
        folder_identity = (None, None)
    payload = [
        review_hash,
        str(resolved),
        folder_identity,
        [entry.model_dump() for entry in entries],
    ]
    plan_id = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return RenamePlan(
        plan_id=plan_id,
        review_hash=review_hash,
        folder=str(resolved),
        folder_dev=folder_identity[0],
        folder_ino=folder_identity[1],
        entries=entries,
    )
