from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

from .config import MAX_UPLOAD_BYTES, UNLOAD_MODELS_AFTER_REQUEST
from .doctr import missing_weight_paths, rectify_document, release_models
from .storage import detect_image_format, image_extension, write_bytes_atomic


app = FastAPI(title="DocTr Stateless Rectification Service", version="0.2.0")
_INFERENCE_LOCK = threading.Lock()


@app.get("/healthz")
def healthz() -> dict:
    missing = [str(path) for path in missing_weight_paths()]
    return {
        "ok": True,
        "queue_size": 0,
        "weights_ready": not missing,
        "missing_weights": missing,
    }


@app.post("/v1/rectify")
async def rectify(file: UploadFile = File(...), ill_rec: bool = Form(False)) -> Response:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="file too large")
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file must not be empty")

    image_format = detect_image_format(data)
    if image_format is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="file must be a supported image")

    with tempfile.TemporaryDirectory(prefix="doctr-rectify-") as tmpdir:
        workdir = Path(tmpdir)
        input_path = workdir / f"input-{uuid.uuid4().hex}{image_extension(file.filename, image_format)}"
        output_path = workdir / "rectified.png"
        write_bytes_atomic(input_path, data)

        try:
            with _INFERENCE_LOCK:
                try:
                    rectify_document(str(input_path), str(output_path), ill_rec=ill_rec)
                finally:
                    if UNLOAD_MODELS_AFTER_REQUEST:
                        release_models()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"DocTr rectification failed: {type(exc).__name__}: {exc}",
            ) from exc

        if not output_path.exists() or not output_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="DocTr rectification did not produce an output image",
            )
        return Response(content=output_path.read_bytes(), media_type="image/png")
