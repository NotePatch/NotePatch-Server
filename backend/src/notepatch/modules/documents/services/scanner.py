from __future__ import annotations

import hashlib
import socket
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from notepatch.platform.config import Settings, get_settings


class DocumentScanError(RuntimeError):
    pass


class MalwareDetectedError(DocumentScanError):
    pass


class ScannerUnavailableError(DocumentScanError):
    pass


@dataclass(frozen=True)
class DocumentScanResult:
    sha256: str
    detected_mime_type: str
    file_size: int
    clamav_signature: str | None = None


class DocumentScanner:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def scan(self, path: Path, declared_mime_type: str | None) -> DocumentScanResult:
        file_size = path.stat().st_size
        maximum = self.settings.upload_max_file_size_mb * 1024 * 1024
        if file_size <= 0:
            raise DocumentScanError("Uploaded file is empty")
        if file_size > maximum:
            raise DocumentScanError(f"File exceeds the {self.settings.upload_max_file_size_mb} MB limit")
        sha256 = self._sha256(path)
        detected = self._detect_mime(path)
        self._validate_mime(declared_mime_type, detected)
        signature = self._clamav_scan(path) if self.settings.clamav_enabled else None
        return DocumentScanResult(sha256, detected, file_size, signature)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _detect_mime(path: Path) -> str:
        try:
            import magic
        except ImportError as exc:
            raise ScannerUnavailableError("python-magic is not installed") from exc
        detected = str(magic.from_file(str(path), mime=True) or "application/octet-stream").lower()
        if detected in {"application/zip", "application/octet-stream"} and zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                if any(name.startswith("word/") for name in names):
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if any(name.startswith("ppt/") for name in names):
                    return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            except (OSError, zipfile.BadZipFile):
                pass
        return detected

    def _validate_mime(self, declared: str | None, detected: str) -> None:
        allowed = self.settings.upload_allowed_mime_type_set
        if detected not in allowed:
            raise DocumentScanError(f"Detected MIME type is not allowed: {detected}")
        declared_value = (declared or "").split(";", 1)[0].strip().lower()
        if declared_value and declared_value not in {"application/octet-stream", detected}:
            raise DocumentScanError(
                f"Declared MIME type {declared_value} does not match detected MIME type {detected}"
            )

    def _clamav_scan(self, path: Path) -> str | None:
        try:
            with socket.create_connection(
                (self.settings.clamav_host, self.settings.clamav_port),
                timeout=self.settings.clamav_timeout_seconds,
            ) as connection:
                connection.settimeout(self.settings.clamav_timeout_seconds)
                connection.sendall(b"zINSTREAM\0")
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        connection.sendall(struct.pack("!I", len(chunk)))
                        connection.sendall(chunk)
                connection.sendall(struct.pack("!I", 0))
                response = connection.recv(4096).decode("utf-8", errors="replace").strip("\0\r\n ")
        except (OSError, TimeoutError) as exc:
            raise ScannerUnavailableError(f"ClamAV is unavailable: {exc}") from exc
        if response.endswith("OK"):
            return None
        if "FOUND" in response:
            signature = response.rsplit(" FOUND", 1)[0].split(":", 1)[-1].strip()
            raise MalwareDetectedError(f"Malware detected: {signature or 'unknown signature'}")
        raise ScannerUnavailableError(f"Unexpected ClamAV response: {response[:200]}")
