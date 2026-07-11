from __future__ import annotations

import os
import re
import uuid
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .config import SUPPORTED_IMAGE_FORMATS


IMAGE_EXTENSION_BY_FORMAT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "TIFF": ".tif",
}
IMAGE_EXTENSIONS_BY_FORMAT = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "WEBP": {".webp"},
    "TIFF": {".tif", ".tiff"},
}
SAFE_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


def detect_image_format(data: bytes) -> str | None:
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            image_format = (image.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if image_format not in SUPPORTED_IMAGE_FORMATS:
        return None
    return image_format


def image_extension(original_filename: str | None, image_format: str) -> str:
    original_ext = Path(original_filename or "").suffix.lower()
    if original_ext in IMAGE_EXTENSIONS_BY_FORMAT.get(image_format, set()):
        return original_ext
    return IMAGE_EXTENSION_BY_FORMAT[image_format]


def non_image_extension(original_filename: str | None) -> str:
    original_ext = Path(original_filename or "").suffix.lower()
    if SAFE_EXTENSION.fullmatch(original_ext):
        return original_ext
    return ".bin"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp_path.open("wb") as fh:
        fh.write(data)
    os.replace(tmp_path, path)
