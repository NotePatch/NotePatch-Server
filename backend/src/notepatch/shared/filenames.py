import re
from pathlib import PurePosixPath


CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str | None) -> str:
    name = CONTROL_CHARS.sub("", filename or "")
    name = name.replace("\\", "/")
    name = PurePosixPath(name).name
    name = name.strip().strip(".")
    name = SAFE_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name or "upload.bin"


def extension_for_filename(filename: str) -> str:
    safe = sanitize_filename(filename)
    if "." not in safe:
        return "bin"
    ext = safe.rsplit(".", 1)[-1].lower()
    return ext or "bin"


def infer_file_type(filename: str, mime_type: str | None = None) -> str:
    mime = (mime_type or "").lower()
    ext = extension_for_filename(filename)
    if mime.startswith("image/") or ext in {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic"}:
        return "image"
    if mime == "application/pdf" or ext == "pdf":
        return "pdf"
    if ext == "docx" or mime.endswith("wordprocessingml.document"):
        return "docx"
    if ext == "pptx" or mime.endswith("presentationml.presentation"):
        return "pptx"
    if mime.startswith("audio/") or ext in {"mp3", "wav", "m4a", "aac", "ogg"}:
        return "audio"
    if mime.startswith("video/") or ext in {"mp4", "mov", "mkv", "avi", "webm"}:
        return "video"
    return "other"
