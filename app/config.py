from pathlib import Path

MODEL = "claude-opus-4-8"
CLASSIFIER_CONCURRENCY = 3
CONFIDENCE_THRESHOLD = 0.7
IMAGE_MAX_EDGE = 1568
JPEG_QUALITY = 85
APP_SUPPORT_DIR = Path.home() / "Library/Application Support/ImageSmartRenamer"
HISTORY_DIR = APP_SUPPORT_DIR / "history"
RULES_FILE = APP_SUPPORT_DIR / "rules.json"
SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
