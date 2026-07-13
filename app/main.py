from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import HISTORY_DIR, RULES_FILE, SUPPORTED_EXTENSIONS
from app.models import (
    CommitRequest,
    JobCreateRequest,
    ManualRenameRequest,
    ReviewItem,
    ReviewUpdateRequest,
    RuleDraftRequest,
    RuleTemplateRequest,
    ScannedImage,
    UndoRequest,
)
from app.services.claude_classifier import ClaudeClassifier
from app.services.folder_picker import MacFolderPicker
from app.services.history_repository import HistoryRepository
from app.services.job_manager import Job, JobManager
from app.services.rename_executor import RenameExecutor, RenameSafetyError
from app.services.rename_planner import (
    PlanConflictError,
    build_rename_plan,
    build_single_rename_plan,
)
from app.services.rule_repository import (
    CorruptRuleLibraryError,
    RuleRepository,
)
from app.services.scanner import scan_folder
from app.services.thumbnail import thumbnail_for_path

_SAFE_UPLOAD_NAME = re.compile(r"^[^/\\]+$")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    folder_picker=None,
    classifier=None,
    history_dir: Path | None = None,
    rules_file: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Image Smart Renamer", docs_url=None, redoc_url=None)
    picker = folder_picker or MacFolderPicker()
    manager = JobManager(classifier or ClaudeClassifier())
    repository = HistoryRepository(history_dir or HISTORY_DIR)
    rule_repository = RuleRepository(rules_file or RULES_FILE)
    executor = RenameExecutor(repository)
    folders: dict[str, Path] = {}
    folder_scans: dict[str, list[ScannedImage]] = {}
    folder_plans: dict[str, object] = {}
    folder_plan_finals: dict[str, dict[str, int]] = {}

    app.state.folder_sessions = folders
    app.state.folder_scans = folder_scans
    app.state.jobs = manager
    app.state.incomplete_manifests = repository.incomplete()

    def load_rule_library():
        try:
            return rule_repository.load()
        except CorruptRuleLibraryError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.get("/api/rules")
    async def get_rules():
        return load_rule_library().model_dump()

    @app.put("/api/rules/draft")
    async def save_rule_draft(request: RuleDraftRequest):
        try:
            return rule_repository.save_draft(list(request.rules), request.template_id).model_dump()
        except CorruptRuleLibraryError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/api/rules/templates")
    async def create_rule_template(request: RuleTemplateRequest):
        try:
            return rule_repository.create_template(request.name, list(request.rules)).model_dump()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except CorruptRuleLibraryError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.put("/api/rules/templates/{template_id}")
    async def update_rule_template(template_id: str, request: RuleTemplateRequest):
        try:
            return rule_repository.update_template(
                template_id, request.name, list(request.rules)
            ).model_dump()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except CorruptRuleLibraryError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.delete("/api/rules/templates/{template_id}")
    async def delete_rule_template(template_id: str):
        try:
            return rule_repository.delete_template(template_id).model_dump()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except CorruptRuleLibraryError as exc:
            raise HTTPException(500, str(exc)) from exc

    @app.post("/api/folders/select")
    async def select_folder():
        folder = await asyncio.to_thread(picker.choose)
        if folder is None:
            raise HTTPException(400, "No folder selected")
        try:
            resolved = folder.resolve(strict=True)
            images = scan_folder(resolved)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HTTPException(400, "Selected folder cannot be scanned") from exc
        folder_id = uuid.uuid4().hex
        folders[folder_id] = resolved
        folder_scans[folder_id] = images
        folder_plans.pop(folder_id, None)
        folder_plan_finals.pop(folder_id, None)
        return {
            "folder_id": folder_id,
            "folder_name": resolved.name,
            "image_count": len(images),
            "mode": "server",
            "images": [_image_payload(image) for image in images],
        }

    @app.post("/api/folders/from-upload")
    async def from_upload(
        folder_name: str = Form(...),
        files: list[UploadFile] = File(...),
    ):
        """Accept images picked in the browser (client machine) into a temp session."""
        if not files:
            raise HTTPException(400, "No files uploaded")
        label = Path(folder_name.strip() or "images").name or "images"
        temp_root = Path(tempfile.mkdtemp(prefix="image-smart-renamer-"))
        dest = temp_root / label
        dest.mkdir(parents=True, exist_ok=True)
        saved = 0
        try:
            for upload in files:
                raw_name = upload.filename or ""
                name = Path(raw_name).name
                if (
                    not name
                    or name.startswith(".")
                    or name.startswith(".image-smart-renamer-")
                    or not _SAFE_UPLOAD_NAME.fullmatch(name)
                    or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS
                ):
                    continue
                target = dest / name
                if target.exists():
                    raise HTTPException(400, f"Duplicate filename: {name}")
                payload = await upload.read()
                if not payload:
                    raise HTTPException(400, f"Empty file: {name}")
                target.write_bytes(payload)
                saved += 1
            if saved == 0:
                raise HTTPException(400, "No supported images uploaded")
            images = scan_folder(dest)
        except HTTPException:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise HTTPException(400, "Selected folder cannot be scanned") from exc
        folder_id = uuid.uuid4().hex
        folders[folder_id] = dest
        folder_scans[folder_id] = images
        folder_plans.pop(folder_id, None)
        folder_plan_finals.pop(folder_id, None)
        return {
            "folder_id": folder_id,
            "folder_name": dest.name,
            "image_count": len(images),
            "mode": "client",
            "images": [_image_payload(image) for image in images],
        }

    @app.get("/api/folders/{folder_id}/images")
    async def list_folder_images(folder_id: str, rescan: bool = False):
        folder = _folder(folders, folder_id)
        images = folder_scans.get(folder_id)
        if images is None:
            raise HTTPException(404, "Folder images not available")
        if rescan:
            try:
                images = _rescan_folder_images(folder, images)
                folder_scans[folder_id] = images
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(409, f"Rescan failed: {exc}") from exc
        return {"images": [_image_payload(image) for image in images]}

    @app.get("/api/folders/{folder_id}/images/{image_id}/thumbnail")
    async def folder_thumbnail(folder_id: str, image_id: str):
        folder = _folder(folders, folder_id)
        image = _folder_image(folder_scans, folder_id, image_id)
        path = folder / image.original_name
        try:
            payload = await asyncio.to_thread(thumbnail_for_path, path)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, "Image is unavailable") from exc
        return Response(
            content=payload,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.post("/api/folders/{folder_id}/plan")
    async def manual_plan(folder_id: str, request: ManualRenameRequest):
        folder = _folder(folders, folder_id)
        images = folder_scans.get(folder_id)
        if images is None:
            raise HTTPException(404, "Folder images not available")
        # Always re-read disk names so previous renames / swaps are reflected.
        try:
            images = _rescan_folder_images(folder, images)
            folder_scans[folder_id] = images
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, f"Rescan failed: {exc}") from exc
        by_id = {image.id: image for image in images}
        selected: list[ScannedImage] = []
        finals: dict[str, int] = {}
        for assignment in request.assignments:
            image = by_id.get(assignment.image_id)
            if image is None:
                raise HTTPException(404, f"Unknown image: {assignment.image_id}")
            if image.scan_error:
                raise HTTPException(400, f"Invalid image cannot be renamed: {image.original_name}")
            selected.append(image)
            finals[image.id] = assignment.number
        try:
            plan = build_rename_plan(
                folder,
                selected,
                finals,
                {path.name for path in folder.iterdir()},
            )
        except (PlanConflictError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        folder_plans[folder_id] = plan
        folder_plan_finals[folder_id] = finals
        return plan.model_dump()

    @app.post("/api/folders/{folder_id}/commit")
    async def manual_commit(folder_id: str, request: CommitRequest):
        folder = _folder(folders, folder_id)
        images = folder_scans.get(folder_id)
        if images is None:
            raise HTTPException(404, "Folder images not available")
        plan = folder_plans.get(folder_id)
        finals = folder_plan_finals.get(folder_id)
        if plan is None or finals is None or plan.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is missing or stale")
        by_id = {image.id: image for image in images}
        review_items = []
        for entry in plan.entries:
            image = by_id.get(entry.item_id)
            if image is None or entry.item_id not in finals:
                raise HTTPException(409, "Plan references unknown image")
            review_items.append(
                ReviewItem(
                    id=image.id,
                    image=image,
                    final_number=finals[entry.item_id],
                    explicitly_reviewed=True,
                )
            )
        try:
            current = build_rename_plan(
                folder,
                [item.image for item in review_items],
                {item.id: item.final_number for item in review_items},
                {path.name for path in folder.iterdir()},
            )
        except (PlanConflictError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if current.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is stale")
        try:
            manifest = executor.commit(folder, current, review_items, rules=[])
        except (OSError, RenameSafetyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        updated = {image.id: image for image in folder_scans[folder_id]}
        for entry in current.entries:
            image = updated.get(entry.item_id)
            if image is None:
                continue
            path = folder / entry.target_name
            try:
                st = path.stat()
                size, mtime_ns = st.st_size, st.st_mtime_ns
            except OSError:
                size, mtime_ns = image.size, image.mtime_ns
            updated[entry.item_id] = image.model_copy(
                update={
                    "original_name": entry.target_name,
                    "size": size,
                    "mtime_ns": mtime_ns,
                }
            )
        folder_scans[folder_id] = [updated[image.id] for image in folder_scans[folder_id]]
        folder_plans.pop(folder_id, None)
        folder_plan_finals.pop(folder_id, None)
        return {
            "manifest_id": manifest.id,
            "status": manifest.status,
            "images": [_image_payload(image) for image in folder_scans[folder_id]],
        }

    @app.post("/api/jobs")
    async def create_job(request: JobCreateRequest):
        folder = _folder(folders, request.folder_id)
        try:
            images = scan_folder(folder)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if not images:
            raise HTTPException(400, "The selected folder has no supported images")
        job = await manager.create(folder, images, request.rules)
        await manager.wait(job.id)
        return {"job_id": job.id, "status": job.status, "total": len(job.items)}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        job = _job(manager, job_id)
        return _job_payload(job)

    @app.get("/api/jobs/{job_id}/items/{item_id}/thumbnail")
    async def thumbnail(job_id: str, item_id: str):
        job = _job(manager, job_id)
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if item is None:
            raise HTTPException(404, "Unknown item")
        path = job.folder / item.image.original_name
        try:
            payload = await asyncio.to_thread(thumbnail_for_path, path)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, "Image is unavailable") from exc
        return Response(
            content=payload,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.patch("/api/jobs/{job_id}/items/{item_id}")
    async def update_review(job_id: str, item_id: str, request: ReviewUpdateRequest):
        job = _job(manager, job_id)
        valid = {rule.number for rule in job.rules}
        if request.final_number not in valid:
            raise HTTPException(422, "Final number is not defined by the rules")
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if item is None:
            raise HTTPException(404, "Unknown item")
        if item.renamed:
            raise HTTPException(409, "Image already renamed")
        item.final_number = request.final_number
        item.explicitly_reviewed = True
        job.plan = None
        job.item_plans.pop(item_id, None)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/items/{item_id}/retry")
    async def retry(job_id: str, item_id: str):
        try:
            job = await manager.retry(job_id, item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _job_payload(job)

    @app.post("/api/jobs/{job_id}/items/{item_id}/plan")
    async def plan_item(job_id: str, item_id: str, request: ReviewUpdateRequest):
        """Plan a single-image rename after the user confirms its number."""
        job = _job(manager, job_id)
        _require_job_folder(job)
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if item is None:
            raise HTTPException(404, "Unknown item")
        if item.renamed:
            raise HTTPException(409, "Image already renamed")
        valid = {rule.number for rule in job.rules}
        if request.final_number not in valid:
            raise HTTPException(422, "Final number is not defined by the rules")
        item.final_number = request.final_number
        item.explicitly_reviewed = True
        job.plan = None
        try:
            current_names = {path.name for path in job.folder.iterdir()}
            plan = build_single_rename_plan(
                job.folder,
                item.image,
                request.final_number,
                current_names,
            )
        except (PlanConflictError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        job.item_plans[item_id] = plan
        return plan.model_dump()

    @app.post("/api/jobs/{job_id}/items/{item_id}/commit")
    async def commit_item(job_id: str, item_id: str, request: CommitRequest):
        """Commit a previously planned single-image rename."""
        job = _job(manager, job_id)
        _require_job_folder(job)
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if item is None:
            raise HTTPException(404, "Unknown item")
        if item.renamed:
            raise HTTPException(409, "Image already renamed")
        plan = job.item_plans.get(item_id)
        if plan is None or plan.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is missing or stale")
        if item.final_number is None:
            raise HTTPException(409, "Image needs a final number")
        try:
            current = build_single_rename_plan(
                job.folder,
                item.image,
                item.final_number,
                {path.name for path in job.folder.iterdir()},
            )
        except (PlanConflictError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        if current.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is stale")
        try:
            manifest = executor.commit(job.folder, current, [item], job.rules)
        except (OSError, RenameSafetyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        entry = current.entries[0]
        _mark_item_renamed(job, item, entry.target_name)
        job.item_plans.pop(item_id, None)
        job.plan = None
        return {
            "manifest_id": manifest.id,
            "status": manifest.status,
            "source_name": entry.source_name,
            "target_name": entry.target_name,
            "no_op": entry.no_op,
            "job": _job_payload(job),
        }

    @app.post("/api/jobs/{job_id}/plan")
    async def plan(job_id: str):
        job = _job(manager, job_id)
        _require_job_folder(job)
        pending = _pending_items(job)
        if not pending:
            raise HTTPException(400, "No remaining images to rename")
        unresolved = [
            item
            for item in pending
            if item.final_number is None
            or (
                item.classification is not None
                and item.classification.status == "needs_review"
                and not item.explicitly_reviewed
            )
        ]
        if unresolved:
            raise HTTPException(409, "All images need a valid reviewed final number")
        try:
            current_names = {path.name for path in job.folder.iterdir()}
            job.plan = build_rename_plan(
                job.folder,
                [item.image for item in pending],
                {item.id: item.final_number for item in pending},
                current_names,
            )
        except (PlanConflictError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return job.plan.model_dump()

    @app.post("/api/jobs/{job_id}/commit")
    async def commit(job_id: str, request: CommitRequest):
        job = _job(manager, job_id)
        _require_job_folder(job)
        if job.plan is None or job.plan.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is missing or stale")
        pending = _pending_items(job)
        if any(
            item.final_number is None
            or (
                item.classification is not None
                and item.classification.status == "needs_review"
                and not item.explicitly_reviewed
            )
            for item in pending
        ):
            raise HTTPException(409, "All images need a valid reviewed final number")
        current = build_rename_plan(
            job.folder,
            [item.image for item in pending],
            {item.id: item.final_number for item in pending},
            {path.name for path in job.folder.iterdir()},
        )
        if current.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is stale")
        try:
            manifest = executor.commit(job.folder, current, pending, job.rules)
        except (OSError, RenameSafetyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        for entry in current.entries:
            item = next((candidate for candidate in job.items if candidate.id == entry.item_id), None)
            if item is not None:
                _mark_item_renamed(job, item, entry.target_name)
        job.plan = None
        return {"manifest_id": manifest.id, "status": manifest.status}

    @app.get("/api/history/incomplete")
    async def incomplete():
        return {"manifests": [manifest.model_dump() for manifest in repository.incomplete()]}

    @app.get("/api/history/latest")
    async def latest(folder_id: str):
        folder = _folder(folders, folder_id)
        manifest = repository.latest_committed(folder)
        return manifest.model_dump() if manifest else {"status": "none"}

    @app.post("/api/history/undo")
    async def undo(request: UndoRequest):
        folder = _folder(folders, request.folder_id)
        try:
            manifest = executor.undo(folder, request.manifest_id)
        except (OSError, RenameSafetyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"manifest_id": manifest.id, "status": "undone"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


def _image_payload(image: ScannedImage) -> dict:
    return {
        "id": image.id,
        "original_name": image.original_name,
        "extension": image.extension,
        "width": image.width,
        "height": image.height,
        "scan_error": image.scan_error,
    }


def _rescan_folder_images(
    folder: Path, previous: list[ScannedImage]
) -> list[ScannedImage]:
    """Refresh names/metadata from disk while keeping stable image ids when possible."""
    fresh = scan_folder(folder)
    by_sha = {}
    for image in fresh:
        by_sha.setdefault(image.sha256, []).append(image)
    used: set[str] = set()
    remapped: list[ScannedImage] = []
    for old in previous:
        candidates = [
            candidate
            for candidate in by_sha.get(old.sha256, [])
            if candidate.original_name not in used
        ]
        match = next(
            (candidate for candidate in candidates if candidate.original_name == old.original_name),
            None,
        )
        if match is None and len(candidates) == 1:
            match = candidates[0]
        if match is None and candidates:
            match = candidates[0]
        if match is None:
            continue
        used.add(match.original_name)
        remapped.append(
            match.model_copy(
                update={
                    "id": old.id,
                }
            )
        )
    # Append brand-new files not seen before
    for image in fresh:
        if image.original_name in used:
            continue
        remapped.append(image)
        used.add(image.original_name)
    return remapped


def _folder_image(
    folder_scans: dict[str, list[ScannedImage]], folder_id: str, image_id: str
) -> ScannedImage:
    images = folder_scans.get(folder_id)
    if images is None:
        raise HTTPException(404, "Folder images not available")
    for image in images:
        if image.id == image_id:
            return image
    raise HTTPException(404, "Unknown image")


def _open_folder_image(folder: Path, name: str) -> int:
    if not name or Path(name).name != name:
        raise HTTPException(400, "Unsafe image path")
    try:
        root = folder.resolve(strict=True)
        image_path = (root / name).resolve(strict=True)
        if image_path.parent != root or not image_path.is_file():
            raise HTTPException(400, "Unsafe image path")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        return os.open(image_path, flags)
    except OSError as exc:
        raise HTTPException(400, "Image is unavailable") from exc


def _open_job_image(job: Job, name: str) -> int:
    if not name or Path(name).name != name:
        raise HTTPException(400, "Unsafe image path")

    # POSIX: open relative to directory fd (no symlink follow).
    if hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
        folder_fd = -1
        image_fd = -1
        try:
            folder_fd = os.open(job.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            folder_stat = os.fstat(folder_fd)
            if (folder_stat.st_dev, folder_stat.st_ino) != job.folder_identity:
                raise HTTPException(400, "Unsafe image path")
            image_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=folder_fd)
            if not stat.S_ISREG(os.fstat(image_fd).st_mode):
                raise HTTPException(400, "Unsafe image path")
            return image_fd
        except OSError as exc:
            if image_fd >= 0:
                os.close(image_fd)
            raise HTTPException(400, "Image is unavailable") from exc
        except HTTPException:
            if image_fd >= 0:
                os.close(image_fd)
            raise
        finally:
            if folder_fd >= 0:
                os.close(folder_fd)

    # Windows / portable fallback (O_DIRECTORY / dir_fd unavailable).
    try:
        folder = job.folder.resolve(strict=True)
        folder_stat = folder.stat()
        if (folder_stat.st_dev, folder_stat.st_ino) != job.folder_identity:
            raise HTTPException(400, "Unsafe image path")
        image_path = (folder / name).resolve(strict=True)
        if image_path.parent != folder or not image_path.is_file():
            raise HTTPException(400, "Unsafe image path")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        return os.open(image_path, flags)
    except OSError as exc:
        raise HTTPException(400, "Image is unavailable") from exc


def _read_and_close(file_descriptor: int, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        while chunk := os.read(file_descriptor, chunk_size):
            yield chunk
    finally:
        os.close(file_descriptor)


def _thumbnail_media_type(extension: str) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(extension, "application/octet-stream")


def _folder(folders: dict[str, Path], folder_id: str) -> Path:
    try:
        return folders[folder_id]
    except KeyError as exc:
        raise HTTPException(404, "Unknown folder session") from exc


def _job(manager: JobManager, job_id: str) -> Job:
    try:
        return manager.get(job_id)
    except KeyError as exc:
        raise HTTPException(404, "Unknown job") from exc


def _require_job_folder(job: Job) -> None:
    try:
        folder_stat = job.folder.stat()
    except OSError as exc:
        raise HTTPException(409, "Scanned folder is unavailable") from exc
    if (folder_stat.st_dev, folder_stat.st_ino) != job.folder_identity:
        raise HTTPException(409, "Scanned folder identity changed")


def _pending_items(job: Job) -> list:
    return [item for item in job.items if not item.renamed]


def _mark_item_renamed(job: Job, item, target_name: str) -> None:
    """Refresh item identity after a successful rename so later plans stay consistent."""
    path = job.folder / target_name
    try:
        stat_result = path.stat()
        size = stat_result.st_size
        mtime_ns = stat_result.st_mtime_ns
    except OSError:
        size = item.image.size
        mtime_ns = item.image.mtime_ns
    item.image = item.image.model_copy(
        update={
            "original_name": target_name,
            "size": size,
            "mtime_ns": mtime_ns,
        }
    )
    item.renamed = True
    item.explicitly_reviewed = True


def _job_payload(job: Job) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "folder_name": job.folder.name,
        "rules": [rule.model_dump() for rule in job.rules],
        "items": [
            {
                "id": item.id,
                "original_name": item.image.original_name,
                "classification": item.classification.model_dump() if item.classification else None,
                "final_number": item.final_number,
                "explicitly_reviewed": item.explicitly_reviewed,
                "renamed": item.renamed,
                "needs_review": (not item.renamed)
                and (
                    item.final_number is None
                    or (
                        item.classification is not None
                        and item.classification.status == "needs_review"
                        and not item.explicitly_reviewed
                    )
                ),
                "duplicate_count": item.duplicate_count,
            }
            for item in job.items
        ],
    }


app = create_app()
