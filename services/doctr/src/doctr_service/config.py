from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(os.getenv("DOCSERVER_BASE_DIR", Path(__file__).resolve().parents[1])).resolve()
DOCTR_ROOT = Path(os.getenv("DOCTR_ROOT", BASE_DIR / "vendor" / "DocTr")).resolve()
MAX_UPLOAD_BYTES = int(os.getenv("DOCSERVER_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
ILLUMINATION_BATCH_SIZE = int(os.getenv("DOCSERVER_ILL_BATCH_SIZE", "64"))
PNG_COMPRESS_LEVEL = int(os.getenv("DOCSERVER_PNG_COMPRESS_LEVEL", "1"))
UNLOAD_MODELS_AFTER_REQUEST = os.getenv("DOCSERVER_UNLOAD_MODELS_AFTER_REQUEST", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "TIFF"}
REQUIRED_WEIGHT_FILES = {
    "segmentation": DOCTR_ROOT / "model_pretrained" / "seg.pth",
    "geometric": DOCTR_ROOT / "model_pretrained" / "geotr.pth",
    "illumination": DOCTR_ROOT / "model_pretrained" / "illtr.pth",
}
