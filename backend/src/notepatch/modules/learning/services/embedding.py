from __future__ import annotations

from collections.abc import Callable

import httpx

from notepatch.platform.config import get_settings
from notepatch.platform.errors import RetryableTaskError
from notepatch.platform.gpu_lease import GpuLeaseService


class EmbeddingClientError(RetryableTaskError):
    pass


class EmbeddingClient:
    def __init__(self, client: httpx.Client | None = None, gpu_lease: GpuLeaseService | None = None) -> None:
        self.settings = get_settings()
        self.client = client or httpx.Client(timeout=self.settings.embedding_timeout_seconds)
        self.gpu_lease = gpu_lease or GpuLeaseService()

    def health(self) -> dict:
        try:
            response = self.client.get(f"{self.settings.embedding_service_url.rstrip('/')}/healthz")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise EmbeddingClientError(f"Embedding service health check failed: {exc}") from exc

    def embed(
        self,
        texts: list[str],
        *,
        owner: str,
        event_callback: Callable[[str, dict], None] | None = None,
    ) -> list[list[float]]:
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input must contain non-empty strings")
        callback = event_callback or (lambda _event, _data: None)
        with self.gpu_lease.lease(owner=owner, event_callback=callback):
            try:
                response = self.client.post(
                    f"{self.settings.embedding_service_url.rstrip('/')}/v1/embeddings",
                    json={"input": [text.strip() for text in texts]},
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise EmbeddingClientError(f"Embedding request failed: {exc}") from exc
        vectors = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingClientError("Embedding service returned an invalid vector count")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.settings.embedding_dimension:
                raise EmbeddingClientError(
                    f"Embedding service returned a vector with unexpected dimension; "
                    f"expected {self.settings.embedding_dimension}"
                )
        return vectors
