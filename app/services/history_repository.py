from __future__ import annotations

import json
import os
from pathlib import Path

from app.models import Manifest


class CorruptHistoryError(RuntimeError):
    pass


class HistoryRepository:
    def __init__(self, root: Path):
        self.root = root.expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, manifest: Manifest) -> None:
        target = self.root / f"{manifest.id}.json"
        temporary = self.root / f".{manifest.id}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(manifest.model_dump(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def list_all(self) -> list[Manifest]:
        manifests = []
        for path in self.root.glob("*.json"):
            try:
                manifests.append(Manifest.model_validate_json(path.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                raise CorruptHistoryError(f"History manifest is corrupt: {path.name}") from exc
        return sorted(manifests, key=lambda item: item.created_at, reverse=True)

    def get(self, manifest_id: str) -> Manifest | None:
        if not manifest_id or manifest_id != Path(manifest_id).name:
            return None
        path = self.root / f"{manifest_id}.json"
        try:
            return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise CorruptHistoryError(f"History manifest is corrupt: {path.name}") from exc

    def latest_committed(self, folder: Path) -> Manifest | None:
        resolved = str(folder.resolve())
        undone_ids = {
            item.manifest_id
            for item in self.list_all()
            if item.operation == "undo" and item.status == "committed"
        }
        return next(
            (
                item
                for item in self.list_all()
                if item.folder == resolved
                and item.operation == "commit"
                and item.status == "committed"
                and item.id not in undone_ids
            ),
            None,
        )

    def undo_attempt(self, manifest_id: str) -> Manifest | None:
        return next(
            (
                item
                for item in self.list_all()
                if item.operation == "undo" and item.manifest_id == manifest_id
            ),
            None,
        )

    def incomplete(self) -> list[Manifest]:
        return [item for item in self.list_all() if item.status in {"prepared", "needs_recovery"}]

    def incomplete_for(self, folder: Path) -> list[Manifest]:
        resolved = str(folder.resolve())
        return [item for item in self.incomplete() if item.folder == resolved]
