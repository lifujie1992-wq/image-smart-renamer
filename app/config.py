import os
import sys
from pathlib import Path

# OpenAI-compatible API (override via environment)
API_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://sub.711bigseller.icu/v1").rstrip("/")
API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")

CLASSIFIER_CONCURRENCY = 3
CONFIDENCE_THRESHOLD = 0.7
IMAGE_MAX_EDGE = 1568
JPEG_QUALITY = 85


def _app_support_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ImageSmartRenamer"
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "ImageSmartRenamer"
    return Path.home() / ".local" / "share" / "ImageSmartRenamer"


APP_SUPPORT_DIR = _app_support_dir()
HISTORY_DIR = APP_SUPPORT_DIR / "history"
RULES_FILE = APP_SUPPORT_DIR / "rules.json"
SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
