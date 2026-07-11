from __future__ import annotations

import importlib
import sys
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


pytest.importorskip("python_multipart")


def fresh_main(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DOCSERVER_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("DOCTR_ROOT", str(tmp_path / "vendor" / "DocTr"))

    for name in list(sys.modules):
        if name == "doctr_service" or name.startswith("doctr_service."):
            del sys.modules[name]
    return importlib.import_module("doctr_service.main")


def tiny_png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_rectify_returns_png_without_auth(tmp_path, monkeypatch):
    main = fresh_main(tmp_path, monkeypatch)

    def fake_rectify(input_path: str, output_path: str, ill_rec: bool = True) -> None:
        assert Path(input_path).exists()
        assert ill_rec is False
        Path(output_path).write_bytes(tiny_png())

    monkeypatch.setattr(main, "rectify_document", fake_rectify)

    with TestClient(main.app) as client:
        response = client.post(
            "/v1/rectify",
            data={"ill_rec": "false"},
            files={"file": ("paper.png", tiny_png(), "image/png")},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content.startswith(b"\x89PNG")


def test_rectify_rejects_empty_non_image_and_too_large(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCSERVER_MAX_UPLOAD_BYTES", "4")
    main = fresh_main(tmp_path, monkeypatch)

    with TestClient(main.app) as client:
        empty = client.post("/v1/rectify", files={"file": ("empty.png", b"", "image/png")})
        non_image = client.post("/v1/rectify", files={"file": ("note.txt", b"abc", "text/plain")})
        too_large = client.post("/v1/rectify", files={"file": ("large.png", b"12345", "image/png")})

    assert empty.status_code == 400
    assert non_image.status_code == 400
    assert too_large.status_code == 413


def test_rectify_reports_model_failure(tmp_path, monkeypatch):
    main = fresh_main(tmp_path, monkeypatch)

    def failing_rectify(input_path: str, output_path: str, ill_rec: bool = True) -> None:
        raise RuntimeError("cuda unavailable")

    monkeypatch.setattr(main, "rectify_document", failing_rectify)

    with TestClient(main.app) as client:
        response = client.post("/v1/rectify", files={"file": ("paper.png", tiny_png(), "image/png")})

    assert response.status_code == 500
    assert "cuda unavailable" in response.json()["detail"]


def test_legacy_routes_are_not_registered(tmp_path, monkeypatch):
    main = fresh_main(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/v1/register", json={"username": "alice", "password": "1"}).status_code == 404
        assert client.post("/v1/upload", files={"file": ("paper.png", tiny_png(), "image/png")}).status_code == 404
        assert client.get("/v1/jobs/job-1").status_code == 404
        assert client.get("/v1/uploads").status_code == 404
        assert client.get("/v1/images/result.png").status_code == 404
        assert client.get("/v1/files/note.pdf").status_code == 404


def test_health_reports_weight_state(tmp_path, monkeypatch):
    main = fresh_main(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        missing = client.get("/healthz")

    assert missing.status_code == 200
    assert missing.json()["ok"] is True
    assert missing.json()["queue_size"] == 0
    assert missing.json()["weights_ready"] is False

    weights_dir = tmp_path / "vendor" / "DocTr" / "model_pretrained"
    weights_dir.mkdir(parents=True)
    for filename in ("seg.pth", "geotr.pth", "illtr.pth"):
        (weights_dir / filename).write_bytes(b"x")

    main = fresh_main(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        ready = client.get("/healthz")

    assert ready.status_code == 200
    assert ready.json()["weights_ready"] is True
    assert ready.json()["missing_weights"] == []
