from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import SUPPORTED_EXTENSIONS
from app.models import ScannedImage


def _sort_key(path: Path) -> tuple[str, str]:
    return (unicodedata.normalize("NFC", path.name).casefold(), path.name)


def scan_folder(folder: Path) -> list[ScannedImage]:
    root = folder.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Selected path is not a directory")
    candidates = sorted(
        (
            path
            for path in root.iterdir()
            if path.suffix.lower() in SUPPORTED_EXTENSIONS
            and not path.is_symlink()
            and path.is_file()
            and not path.name.startswith(".image-smart-renamer-")
        ),
        key=_sort_key,
    )
    result: list[ScannedImage] = []
    for index, path in enumerate(candidates):
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"File changed while scanning: {path.name}")
        width = height = None
        scan_error = None
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        except (UnidentifiedImageError, OSError, ValueError):
            scan_error = "invalid_image"
        result.append(
            ScannedImage(
                id=f"img-{index}-{hashlib.sha256(path.name.encode()).hexdigest()[:12]}",
                original_name=path.name,
                extension=path.suffix.lower(),
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                width=width,
                height=height,
                sha256=hashlib.sha256(payload).hexdigest(),
                scan_error=scan_error,
            )
        )
    return result
