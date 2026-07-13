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
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import HISTORY_DIR, RULES_FILE, SUPPORTED_EXTENSIONS
from app.models import (
    CommitRequest,
    JobCreateRequest,
    ReviewUpdateRequest,
    RuleDraftRequest,
    RuleTemplateRequest,
    UndoRequest,
)
from app.services.claude_classifier import ClaudeClassifier
from app.services.folder_picker import MacFolderPicker
from app.services.history_repository import HistoryRepository
from app.services.job_manager import Job, JobManager
from app.services.rename_executor import RenameExecutor, RenameSafetyError
from app.services.rename_planner import PlanConflictError, build_rename_plan
from app.services.rule_repository import (
    CorruptRuleLibraryError,
    RuleRepository,
)
from app.services.scanner import scan_folder

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

    app.state.folder_sessions = folders
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
            count = len(scan_folder(resolved))
        except (OSError, ValueError) as exc:
            raise HTTPException(400, "Selected folder cannot be scanned") from exc
        folder_id = uuid.uuid4().hex
        folders[folder_id] = resolved
        return {
            "folder_id": folder_id,
            "folder_name": resolved.name,
            "image_count": count,
            "mode": "server",
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
            count = len(scan_folder(dest))
        except HTTPException:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise HTTPException(400, "Selected folder cannot be scanned") from exc
        folder_id = uuid.uuid4().hex
        folders[folder_id] = dest
        return {
            "folder_id": folder_id,
            "folder_name": dest.name,
            "image_count": count,
            "mode": "client",
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
        image_fd = _open_job_image(job, item.image.original_name)
        return StreamingResponse(
            _read_and_close(image_fd),
            media_type=_thumbnail_media_type(item.image.extension),
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
        item.final_number = request.final_number
        item.explicitly_reviewed = True
        job.plan = None
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/items/{item_id}/retry")
    async def retry(job_id: str, item_id: str):
        try:
            job = await manager.retry(job_id, item_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _job_payload(job)

    @app.post("/api/jobs/{job_id}/plan")
    async def plan(job_id: str):
        job = _job(manager, job_id)
        _require_job_folder(job)
        unresolved = [
            item
            for item in job.items
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
                [item.image for item in job.items],
                {item.id: item.final_number for item in job.items},
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
        if any(
            item.final_number is None
            or (
                item.classification is not None
                and item.classification.status == "needs_review"
                and not item.explicitly_reviewed
            )
            for item in job.items
        ):
            raise HTTPException(409, "All images need a valid reviewed final number")
        current = build_rename_plan(
            job.folder,
            [item.image for item in job.items],
            {item.id: item.final_number for item in job.items},
            {path.name for path in job.folder.iterdir()},
        )
        if current.plan_id != request.plan_id:
            raise HTTPException(409, "Rename plan is stale")
        try:
            manifest = executor.commit(job.folder, current, job.items, job.rules)
        except (OSError, RenameSafetyError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
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


def _open_job_image(job: Job, name: str) -> int:
    if not name or Path(name).name != name:
        raise HTTPException(400, "Unsafe image path")
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
                "needs_review": item.final_number is None
                or (
                    item.classification is not None
                    and item.classification.status == "needs_review"
                    and not item.explicitly_reviewed
                ),
                "duplicate_count": item.duplicate_count,
            }
            for item in job.items
        ],
    }


app = create_app()
