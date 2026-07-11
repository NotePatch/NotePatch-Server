from __future__ import annotations

import shutil
import struct
from pathlib import Path

from notepatch.modules.documents.ocr.base import OcrOptions


class ImagePreprocessor:
    def preprocess(self, image_path: Path, output_path: Path, *, options: OcrOptions) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not options.enable_preprocess:
            if image_path != output_path:
                shutil.copyfile(image_path, output_path)
            return output_path
        try:
            from PIL import Image, ImageOps  # type: ignore
        except Exception:
            if image_path != output_path:
                shutil.copyfile(image_path, output_path)
            return output_path

        try:
            with Image.open(image_path) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                elif image.mode == "RGBA":
                    image = image.convert("RGB")
                image.save(output_path)
        except Exception:
            if image_path != output_path:
                shutil.copyfile(image_path, output_path)
        return output_path


def image_size(image_path: Path) -> tuple[int, int]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(image_path) as image:
            return image.size
    except Exception:
        return _image_size_without_pillow(image_path)


def _image_size_without_pillow(image_path: Path) -> tuple[int, int]:
    try:
        header = image_path.read_bytes()[:64]
    except OSError:
        return (1, 1)

    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
        return struct.unpack(">II", header[16:24])

    if header.startswith(b"\xff\xd8"):
        return _jpeg_size(image_path)

    return (1, 1)


def _jpeg_size(image_path: Path) -> tuple[int, int]:
    try:
        with image_path.open("rb") as file:
            file.read(2)
            while True:
                marker_start = file.read(1)
                if marker_start != b"\xff":
                    return (1, 1)
                marker = file.read(1)
                while marker == b"\xff":
                    marker = file.read(1)
                if marker in {b"\xc0", b"\xc1", b"\xc2", b"\xc3"}:
                    file.read(3)
                    height, width = struct.unpack(">HH", file.read(4))
                    return (width, height)
                length_bytes = file.read(2)
                if len(length_bytes) != 2:
                    return (1, 1)
                length = struct.unpack(">H", length_bytes)[0]
                file.seek(length - 2, 1)
    except Exception:
        return (1, 1)
