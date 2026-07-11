from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

import httpx

from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError


class DocTrClientError(RetryableTaskError):
    pass


class DocTrClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.doctr_base_url).rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds or settings.doctr_timeout_seconds

    def health(self) -> dict:
        try:
            response = httpx.get(urljoin(self.base_url, "healthz"), timeout=10)
        except httpx.HTTPError as exc:
            raise DocTrClientError(f"DocTr health check failed: {exc}") from exc
        if response.status_code >= 400:
            raise DocTrClientError(f"DocTr health check failed with HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        if not payload.get("weights_ready"):
            raise DocTrClientError(f"DocTr model weights are not ready: {payload.get('missing_weights')}")
        return payload

    def rectify_image(
        self,
        file_path: str | Path,
        output_path: str | Path,
        *,
        filename: str,
        content_type: str | None = None,
        ill_rec: bool = True,
    ) -> None:
        path = Path(file_path)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("rb") as fh:
                with httpx.stream(
                    "POST",
                    urljoin(self.base_url, "v1/rectify"),
                    data={"ill_rec": "true" if ill_rec else "false"},
                    files={"file": (filename, fh, content_type or "application/octet-stream")},
                    timeout=self.timeout_seconds,
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", errors="replace")
                        raise DocTrClientError(
                            f"DocTr rectification failed with HTTP {response.status_code}: {body[:300]}"
                        )
                    content_type_header = response.headers.get("content-type", "")
                    if not content_type_header.startswith("image/png"):
                        raise DocTrClientError(f"DocTr returned unexpected content type: {content_type_header}")
                    with output.open("wb") as fh_out:
                        for chunk in response.iter_bytes():
                            fh_out.write(chunk)
        except httpx.HTTPError as exc:
            raise DocTrClientError(f"DocTr rectification failed: {exc}") from exc
        if not output.exists() or output.stat().st_size == 0:
            raise DocTrClientError("DocTr returned an empty result")


def get_doctr_client() -> DocTrClient:
    return DocTrClient()
