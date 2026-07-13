from __future__ import annotations

import asyncio
import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Classification, ReviewItem, Rule, ScannedImage
from app.services.duplicate_detector import group_exact_duplicates
from app.services.image_encoder import encode_for_claude


@dataclass
class Job:
    id: str
    folder: Path
    rules: list[Rule]
    items: list[ReviewItem]
    folder_identity: tuple[int, int]
    status: str = "processing"
    plan: object | None = None
    item_plans: dict = field(default_factory=dict)
    task: asyncio.Task | None = field(default=None, repr=False)


class JobManager:
    def __init__(self, classifier, encoder=encode_for_claude, concurrency: int = 3):
        self.classifier = classifier
        self.encoder = encoder
        self.semaphore = asyncio.Semaphore(concurrency)
        self.jobs: dict[str, Job] = {}

    async def create(self, folder: Path, images: list[ScannedImage], rules: list[Rule]) -> Job:
        counts = {group[0].sha256: len(group) for group in group_exact_duplicates(images)}
        items = [
            ReviewItem(id=image.id, image=image, duplicate_count=counts[image.sha256])
            for image in images
        ]
        stat = folder.stat()
        job = Job(
            id=uuid.uuid4().hex,
            folder=folder,
            rules=rules,
            items=items,
            folder_identity=(stat.st_dev, stat.st_ino),
        )
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job))
        return job

    async def wait(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.task:
            await job.task
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise KeyError("Unknown job") from exc

    async def _run(self, job: Job) -> None:
        groups: dict[str, list[ReviewItem]] = {}
        for item in job.items:
            groups.setdefault(item.image.sha256, []).append(item)
        await asyncio.gather(*(self._classify_group(job, group) for group in groups.values()))
        job.status = "completed"

    async def _classify_group(self, job: Job, group: list[ReviewItem]) -> None:
        representative = group[0]
        if representative.image.scan_error:
            result = Classification(
                status="needs_review",
                confidence=0,
                reason="Image cannot be decoded",
                error_code="invalid_image",
            )
        else:
            try:
                async with self.semaphore:
                    encoded = self.encoder(job.folder / representative.image.original_name)
                    if inspect.isawaitable(encoded):
                        encoded = await encoded
                    result = await self.classifier.classify(encoded, job.rules)
            except Exception:
                result = Classification(
                    status="needs_review",
                    confidence=0,
                    reason="Classification failed",
                    error_code="classifier_failure",
                )
        for item in group:
            item.classification = result
            if result.status == "matched":
                item.final_number = result.predicted_number

    async def retry(self, job_id: str, item_id: str) -> Job:
        job = self.get(job_id)
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if item is None:
            raise KeyError("Unknown item")
        group = [
            candidate for candidate in job.items if candidate.image.sha256 == item.image.sha256
        ]
        for candidate in group:
            candidate.final_number = None
            candidate.explicitly_reviewed = False
        job.plan = None
        await self._classify_group(job, group)
        return job
