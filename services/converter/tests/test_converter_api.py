from pathlib import Path
from types import SimpleNamespace
import subprocess

from fastapi.testclient import TestClient

from converter_service import main


client = TestClient(main.app)


def test_health_reports_soffice(monkeypatch):
    monkeypatch.setattr(main.shutil, "which", lambda _name: "/usr/bin/soffice")
    assert client.get("/healthz").json() == {"ok": True, "soffice": "/usr/bin/soffice"}


def test_convert_docx_returns_pdf(monkeypatch):
    def fake_run(command, **_kwargs):
        output_dir = Path(command[command.index("--outdir") + 1])
        (output_dir / "input.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)
    response = client.post(
        "/v1/convert",
        files={"file": ("notes.docx", b"valid office bytes", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"output_format": "pdf"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")


def test_damaged_office_file_returns_422(monkeypatch):
    monkeypatch.setattr(
        main.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="damaged document"),
    )
    response = client.post("/v1/convert", files={"file": ("bad.pptx", b"bad", "application/octet-stream")})
    assert response.status_code == 422
    assert "damaged" in response.json()["detail"]


def test_conversion_timeout_returns_504(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("soffice", 150)

    monkeypatch.setattr(main.subprocess, "run", timeout)
    response = client.post("/v1/convert", files={"file": ("slow.docx", b"data", "application/octet-stream")})
    assert response.status_code == 504


def test_size_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(main, "_MAX_BYTES", 2)
    response = client.post("/v1/convert", files={"file": ("large.docx", b"123", "application/octet-stream")})
    assert response.status_code == 413
