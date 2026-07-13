from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image, ImageOps

from app.config import IMAGE_MAX_EDGE, JPEG_QUALITY
from app.models import EncodedImage


def encode_for_claude(path: Path, max_edge: int = IMAGE_MAX_EDGE) -> EncodedImage:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return EncodedImage(
            data=base64.b64encode(buffer.getvalue()).decode("ascii"),
            width=image.width,
            height=image.height,
        )
