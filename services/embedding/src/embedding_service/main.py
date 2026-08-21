from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class EmbeddingRequest(BaseModel):
    input: list[str] = Field(min_length=1, max_length=128)


class EmbeddingResponse(BaseModel):
    model: str
    dimension: int
    embeddings: list[list[float]]


class ModelState:
    def __init__(self) -> None:
        self.model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
        self.device = os.getenv("EMBEDDING_DEVICE", "cpu").strip().lower() or "cpu"
        self.model = None
        self.error: str | None = None
        self.lock = threading.Lock()

    def load(self) -> None:
        try:
            from FlagEmbedding import BGEM3FlagModel

            self.model = BGEM3FlagModel(
                self.model_name,
                use_fp16=self.device.startswith("cuda"),
                devices=[self.device],
            )
        except Exception as exc:
            self.error = str(exc)
            raise


state = ModelState()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    state.load()
    yield


app = FastAPI(title="NotePatch BGE-M3 Embedding Service", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict:
    return {
        "ok": state.model is not None,
        "model": state.model_name,
        "device": state.device,
        "dimension": 1024,
        "error": state.error,
    }


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def embeddings(payload: EmbeddingRequest) -> EmbeddingResponse:
    if state.model is None:
        raise HTTPException(status_code=503, detail=state.error or "Embedding model is not ready")
    texts = [text.strip() for text in payload.input]
    if any(not text for text in texts):
        raise HTTPException(status_code=400, detail="Embedding input cannot be empty")
    with state.lock:
        result = state.model.encode(texts, batch_size=min(16, len(texts)), max_length=8192)
    vectors = result.get("dense_vecs") if isinstance(result, dict) else None
    if vectors is None:
        raise HTTPException(status_code=500, detail="BGE-M3 did not return dense vectors")
    values = vectors.tolist() if hasattr(vectors, "tolist") else vectors
    return EmbeddingResponse(model=state.model_name, dimension=1024, embeddings=values)
