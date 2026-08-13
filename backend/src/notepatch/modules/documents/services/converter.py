from __future__ import annotations

from pathlib import Path

import httpx

from notepatch.platform.config import Settings, get_settings
from notepatch.platform.errors import PermanentTaskError, RetryableTaskError


class DocumentConverterClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def convert_to_pdf(self, source: Path, destination: Path, *, filename: str, mime_type: str | None) -> None:
        try:
            with source.open("rb") as stream:
                response = httpx.post(
                    f"{self.settings.converter_base_url.rstrip('/')}/v1/convert",
                    data={"output_format": "pdf"},
                    files={"file": (filename, stream, mime_type or "application/octet-stream")},
                    timeout=self.settings.converter_timeout_seconds,
                )
        except httpx.HTTPError as exc:
            raise RetryableTaskError(f"Document converter is unavailable: {exc}") from exc
        if response.status_code >= 500:
            raise RetryableTaskError(
                f"Document converter returned HTTP {response.status_code}: {response.text[:300]}"
            )
        if response.status_code != 200:
            raise PermanentTaskError(
                f"Document conversion failed with HTTP {response.status_code}: {response.text[:300]}"
            )
        if not response.content.startswith(b"%PDF"):
            raise PermanentTaskError("Document converter returned an invalid PDF")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(response.content)
