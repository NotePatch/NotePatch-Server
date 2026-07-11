import base64
import hmac
from hashlib import sha256
from pathlib import Path

import httpx

from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError


class TusdService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def base_url(self) -> str:
        return self.settings.tusd_base_url.rstrip("/") + "/"

    def build_upload_url(self, tus_upload_id: str | None) -> str | None:
        if not tus_upload_id:
            return None
        return self.base_url + tus_upload_id

    def sign_upload(self, upload_session_id: str, document_id: str, object_key: str) -> str:
        payload = f"{upload_session_id}:{document_id}:{object_key}".encode("utf-8")
        secret = self.settings.tusd_webhook_secret.encode("utf-8")
        return hmac.new(secret, payload, sha256).hexdigest()

    def verify_upload_token(self, upload_session_id: str, document_id: str, object_key: str, token: str | None) -> bool:
        if not token:
            return False
        expected = self.sign_upload(upload_session_id, document_id, object_key)
        return hmac.compare_digest(expected, token)

    def build_metadata(self, *, upload_session_id: str, document_id: str, object_key: str, filename: str, mime_type: str | None) -> dict[str, str]:
        return {
            "upload_session_id": upload_session_id,
            "document_id": document_id,
            "upload_token": self.sign_upload(upload_session_id, document_id, object_key),
            "filename": filename,
            "mime_type": mime_type or "application/octet-stream",
        }

    def metadata_header(self, metadata: dict[str, str]) -> str:
        parts = []
        for key, value in metadata.items():
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
            parts.append(f"{key} {encoded}")
        return ",".join(parts)

    def local_file_path(self, tus_upload_id: str, storage_path: str | None = None) -> Path:
        if storage_path:
            return Path(self.settings.tusd_data_dir) / Path(storage_path).name
        return Path(self.settings.tusd_data_dir) / tus_upload_id

    def terminate_upload(self, tus_upload_id: str) -> None:
        if not tus_upload_id:
            return
        url = self.settings.tusd_internal_base_url.rstrip("/") + f"/{tus_upload_id}"
        try:
            response = httpx.delete(
                url,
                headers={"Tus-Resumable": "1.0.0"},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise RetryableTaskError(f"Could not terminate tusd upload {tus_upload_id}: {exc}") from exc
        if response.status_code not in {204, 404, 410}:
            raise RetryableTaskError(
                f"Could not terminate tusd upload {tus_upload_id}: HTTP {response.status_code}"
            )
