from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

app = FastAPI(title="NotePatch Office Converter", docs_url=None, redoc_url=None)
_lock = threading.Lock()
_ALLOWED = {".docx", ".pptx"}
_MAX_BYTES = 200 * 1024 * 1024


@app.get("/healthz")
def health() -> dict:
    executable = shutil.which("soffice")
    return {"ok": executable is not None, "soffice": executable}


@app.post("/v1/convert")
def convert_document(
    file: UploadFile = File(...),
    output_format: str = Form(default="pdf"),
) -> Response:
    if output_format.lower() != "pdf":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Only PDF output is supported")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _ALLOWED:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Only DOCX and PPTX are supported")
    with tempfile.TemporaryDirectory(prefix="notepatch-converter-") as tmpdir:
        workdir = Path(tmpdir)
        source = workdir / f"input{suffix}"
        size = 0
        with source.open("wb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File is too large")
                output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is empty")
        with _lock:
            try:
                completed = subprocess.run(
                    ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(workdir), str(source)],
                    capture_output=True,
                    text=True,
                    timeout=150,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="Office conversion timed out") from exc
        result = workdir / "input.pdf"
        if completed.returncode != 0 or not result.exists() or result.stat().st_size == 0:
            detail = (completed.stderr or completed.stdout or "Office conversion failed")[-500:]
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
        return Response(
            result.read_bytes(),
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="converted.pdf"'},
        )
