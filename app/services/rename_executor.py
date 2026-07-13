from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from app.models import Manifest, RenamePlan, ReviewItem
from app.services.history_repository import CorruptHistoryError, HistoryRepository


class RenameSafetyError(RuntimeError):
    pass


class RenameExecutor:
    def __init__(self, repository: HistoryRepository, fail_after_steps: int | None = None):
        self.repository = repository
        self.fail_after_steps = fail_after_steps
        self.steps = 0

    def commit(
        self,
        folder: Path,
        plan: RenamePlan,
        items: list[ReviewItem],
        rules,
    ) -> Manifest:
        root = folder.resolve(strict=True)
        self._require_no_incomplete(root)
        root_stat = root.stat()
        if (
            str(root) != plan.folder
            or (plan.folder_dev, plan.folder_ino) != (root_stat.st_dev, root_stat.st_ino)
            or not os.access(root, os.W_OK)
        ):
            raise RenameSafetyError("Folder does not match plan or is not writable")
        self._validate_sources(root, plan)
        self._validate_destinations(root, plan, source_mode=True)
        manifest = Manifest(
            id=uuid.uuid4().hex,
            folder=str(root),
            status="prepared",
            plan_id=plan.plan_id,
            rules=[rule.model_dump() if hasattr(rule, "model_dump") else rule for rule in rules],
            entries=[
                {
                    **entry.model_dump(),
                    "classification": (
                        review.classification.model_dump()
                        if getattr(review, "classification", None)
                        else None
                    ),
                    "final_number": getattr(review, "final_number", None),
                    "explicitly_reviewed": getattr(review, "explicitly_reviewed", False),
                }
                for entry in plan.entries
                for review in items
                if review.id == entry.item_id
            ],
        )
        self.repository.save(manifest)
        try:
            self._execute(root, manifest, forward=True)
            manifest.status = "committed"
            self.repository.save(manifest)
            return manifest
        except Exception as exc:
            manifest.error = str(exc)
            if self._rollback(root, manifest, forward=True):
                manifest.status = "prepared"
            else:
                manifest.status = "needs_recovery"
            self.repository.save(manifest)
            raise

    def undo_latest(self, folder: Path) -> Manifest:
        root = folder.resolve(strict=True)
        manifest = self.repository.latest_committed(root)
        if manifest is None:
            raise RenameSafetyError("No committed operation is available to undo")
        self.undo(root, manifest.id)
        updated = self.repository.get(manifest.id)
        if updated is None:
            raise RenameSafetyError("Undone manifest is unavailable")
        updated.status = "undone"
        self.repository.save(updated)
        return updated

    def undo(self, folder: Path, manifest_id: str) -> Manifest:
        root = folder.resolve(strict=True)
        existing = self.repository.undo_attempt(manifest_id)
        if existing is not None and existing.status == "committed":
            return existing
        self._require_no_incomplete(root)
        manifest = self.repository.get(manifest_id)
        if (
            manifest is None
            or manifest.folder != str(root)
            or manifest.operation != "commit"
            or manifest.status != "committed"
        ):
            raise RenameSafetyError("Committed manifest is unavailable to undo")
        self._validate_undo(root, manifest)
        attempt = Manifest(
            id=uuid.uuid4().hex,
            operation="undo",
            manifest_id=manifest.id,
            folder=manifest.folder,
            status="prepared",
            plan_id=manifest.plan_id,
            rules=manifest.rules,
            entries=manifest.entries,
        )
        self.repository.save(attempt)
        try:
            self._execute(root, attempt, forward=False)
            attempt.status = "committed"
            self.repository.save(attempt)
            return attempt
        except Exception as exc:
            attempt.error = str(exc)
            if self._rollback(root, attempt, forward=False):
                attempt.status = "rolled_back"
            else:
                attempt.status = "needs_recovery"
            self.repository.save(attempt)
            raise

    def _validate_sources(self, root: Path, plan: RenamePlan) -> None:
        for entry in plan.entries:
            path = self._safe_path(root, entry.source_name)
            if not path.is_file() or path.is_symlink():
                raise RenameSafetyError(f"Source is missing or unsafe: {entry.source_name}")
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if (stat.st_size, stat.st_mtime_ns, digest) != (
                entry.size,
                entry.mtime_ns,
                entry.sha256,
            ):
                raise RenameSafetyError(f"Source changed after scanning: {entry.source_name}")

    def _validate_destinations(self, root: Path, plan: RenamePlan, source_mode: bool) -> None:
        sources = {entry.source_name for entry in plan.entries if not entry.no_op}
        for entry in plan.entries:
            if entry.no_op:
                continue
            target = self._safe_path(root, entry.target_name)
            temporary = self._safe_path(root, entry.temporary_name)
            if temporary.exists():
                raise RenameSafetyError("Temporary name already exists")
            if target.exists() and target.name not in sources:
                raise RenameSafetyError(f"Target is occupied: {target.name}")

    def _validate_undo(self, root: Path, manifest: Manifest) -> None:
        targets = {entry["target_name"] for entry in manifest.entries if not entry["no_op"]}
        for entry in manifest.entries:
            if entry["no_op"]:
                continue
            current = self._safe_path(root, entry["target_name"])
            original = self._safe_path(root, entry["source_name"])
            if (
                not current.is_file()
                or hashlib.sha256(current.read_bytes()).hexdigest() != entry["sha256"]
            ):
                raise RenameSafetyError(f"Renamed file changed: {current.name}")
            if original.exists() and original.name not in targets:
                raise RenameSafetyError(f"Original name is occupied: {original.name}")

    def _execute(self, root: Path, manifest: Manifest, forward: bool) -> None:
        active = [entry for entry in manifest.entries if not entry["no_op"]]
        for entry in active:
            source_name = entry["source_name"] if forward else entry["target_name"]
            temporary_name = self._undo_temp(entry) if not forward else entry["temporary_name"]
            step = {"from": source_name, "to": temporary_name, "applied": False}
            manifest.completed_steps.append(step)
            self.repository.save(manifest)
            self._rename_exclusive(root, source_name, temporary_name)
            step["applied"] = True
            self.repository.save(manifest)
            self._maybe_fail()
        for entry in active:
            temporary_name = self._undo_temp(entry) if not forward else entry["temporary_name"]
            target_name = entry["target_name"] if forward else entry["source_name"]
            step = {"from": temporary_name, "to": target_name, "applied": False}
            manifest.completed_steps.append(step)
            self.repository.save(manifest)
            self._rename_exclusive(root, temporary_name, target_name)
            step["applied"] = True
            self.repository.save(manifest)
            self._maybe_fail()

    def recover_incomplete(self, folder: Path) -> list[Manifest]:
        root = folder.resolve(strict=True)
        recovered = []
        for manifest in self.repository.incomplete_for(root):
            if self._rollback(root, manifest, forward=True):
                manifest.status = "rolled_back"
                manifest.error = None
            else:
                manifest.status = "needs_recovery"
            self.repository.save(manifest)
            recovered.append(manifest)
        return recovered

    def _require_no_incomplete(self, root: Path) -> None:
        try:
            incomplete = self.repository.incomplete_for(root)
        except CorruptHistoryError as exc:
            raise RenameSafetyError(str(exc)) from exc
        if incomplete:
            raise RenameSafetyError("Folder has an unfinished rename operation")

    def _rollback(self, root: Path, manifest: Manifest, forward: bool) -> bool:
        try:
            for step in reversed(manifest.completed_steps):
                destination = self._safe_path(root, step["to"])
                source = self._safe_path(root, step["from"])
                if destination.exists() and not source.exists():
                    self._rename_exclusive(root, step["to"], step["from"])
                elif source.exists() and not destination.exists():
                    continue
                elif not step.get("applied", True) and source.exists() and destination.exists():
                    continue
                else:
                    return False
            manifest.completed_steps.clear()
            return True
        except Exception:
            return False

    @staticmethod
    def _safe_path(root: Path, name: str) -> Path:
        if not name or name in {".", ".."} or Path(name).name != name:
            raise RenameSafetyError("Unsafe filename")
        candidate = root / name
        if candidate.parent.resolve() != root:
            raise RenameSafetyError("Path escapes selected folder")
        return candidate

    def _rename_exclusive(self, root: Path, source_name: str, target_name: str) -> None:
        source = self._safe_path(root, source_name)
        target = self._safe_path(root, target_name)
        if not source.exists():
            raise RenameSafetyError(f"Unsafe rename {source_name} -> {target_name}")
        try:
            os.link(source, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise RenameSafetyError(f"Unsafe rename {source_name} -> {target_name}") from exc
        try:
            source.unlink()
        except OSError:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def _undo_temp(entry: dict) -> str:
        return f".image-smart-renamer-undo-{entry['item_id']}"

    def _maybe_fail(self) -> None:
        self.steps += 1
        if self.fail_after_steps is not None and self.steps >= self.fail_after_steps:
            raise RuntimeError("Injected rename failure")
