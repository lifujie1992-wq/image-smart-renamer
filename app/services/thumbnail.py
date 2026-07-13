from __future__ import annotations

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

# Small enough for grid preview; keeps gallery snappy.
THUMB_MAX_EDGE = 240
THUMB_QUALITY = 52


def make_thumbnail_jpeg(
    path: Path,
    *,
    max_edge: int = THUMB_MAX_EDGE,
    quality: int = THUMB_QUALITY,
) -> bytes:
    """Return a low-res JPEG suitable for UI preview only."""
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((max_edge, max_edge), Image.Resampling.BILINEAR)
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (245, 247, 244))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        return buffer.getvalue()


@lru_cache(maxsize=512)
def cached_thumbnail_jpeg(path_str: str, mtime_ns: int, size: int) -> bytes:
    """Cache by path + identity so re-renders do not re-decode originals."""
    return make_thumbnail_jpeg(Path(path_str))


def thumbnail_for_path(path: Path) -> bytes:
    try:
        st = path.stat()
        return cached_thumbnail_jpeg(str(path.resolve()), st.st_mtime_ns, st.st_size)
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError(f"Cannot build thumbnail: {path.name}") from exc
