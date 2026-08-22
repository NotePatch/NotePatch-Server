import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner
from notepatch.modules.documents.services.upload import _validate_upload_format
from notepatch.platform.config import get_settings


SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "docker" / "openclaw-file-tools" / "notepatch_file.py"


def _run_file_tool(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        env={**os.environ, "OPENCLAW_WORKSPACE_ROOT": str(workspace)},
        check=False,
    )


def test_file_tool_extracts_task_local_text(tmp_path):
    source = tmp_path / "lesson.md"
    source.write_text("# Binary shifts\n\nA logical shift inserts zero bits.", encoding="utf-8")
    output = tmp_path / "notepatch" / "openclaw" / "tasks" / "task-1" / "output" / "parser" / "doc-1"

    result = _run_file_tool(tmp_path, "extract", str(source), "--output-dir", str(output))

    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["category"] == "text"
    assert "logical shift" in (output / "content.md").read_text(encoding="utf-8")


def test_file_tool_rejects_output_outside_task_parser(tmp_path):
    source = tmp_path / "lesson.txt"
    source.write_text("content", encoding="utf-8")

    result = _run_file_tool(tmp_path, "extract", str(source), "--output-dir", str(tmp_path / "elsewhere"))

    assert result.returncode == 2
    assert "output/parser" in result.stderr


def test_file_tool_rejects_archive_path_traversal(tmp_path):
    source = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../escape.txt", "nope")

    result = _run_file_tool(tmp_path, "list", str(source))

    assert result.returncode == 2
    assert "Unsafe archive member" in result.stderr


def test_extended_chat_attachment_is_allowed():
    _validate_upload_format(
        filename="notes.epub",
        mime_type="application/epub+zip",
        file_type="other",
        document_kind="chat_attachment",
        allowed_mime_types=get_settings().upload_allowed_mime_type_set,
    )


def test_extended_learning_document_is_rejected():
    with pytest.raises(Exception) as error:
        _validate_upload_format(
            filename="notes.epub",
            mime_type="application/epub+zip",
            file_type="other",
            document_kind="note",
            allowed_mime_types=get_settings().upload_allowed_mime_type_set,
        )
    assert getattr(error.value, "status_code", None) == 422
    assert error.value.detail["code"] == "unsupported_learning_format"


def test_unknown_upload_format_is_rejected():
    with pytest.raises(Exception) as error:
        _validate_upload_format(
            filename="payload.unknown",
            mime_type="application/x-unknown",
            file_type="other",
            document_kind="chat_attachment",
            allowed_mime_types=get_settings().upload_allowed_mime_type_set,
        )
    assert getattr(error.value, "status_code", None) == 415
    assert error.value.detail["code"] == "unsupported_file_format"


def test_gateway_context_names_attached_binary_and_parser_output():
    note = OpenClawGatewayRunner._context_note(
        {
            "documents_index_path": "/workspace/tasks/t1/input/documents/index.json",
            "documents_root_path": "/workspace/tasks/t1/input/documents",
            "task_output_path": "/workspace/tasks/t1/output",
            "attachment_files": [
                {
                    "document_id": "doc-1",
                    "filename": "lesson.epub",
                    "original_path": "/workspace/tasks/t1/input/documents/doc-1/original/lesson.epub",
                }
            ],
        }
    )

    assert "lesson.epub" in note
    assert "document_id=doc-1" in note
    assert "notepatch-file extract" in note
    assert "/workspace/tasks/t1/output/parser/" in note


def test_executable_extension_cannot_hide_behind_text_mime():
    with pytest.raises(Exception) as error:
        _validate_upload_format(
            filename="payload.exe",
            mime_type="text/plain",
            file_type="other",
            document_kind="chat_attachment",
            allowed_mime_types=get_settings().upload_allowed_mime_type_set,
        )
    assert getattr(error.value, "status_code", None) == 415
    assert error.value.detail["code"] == "unsupported_file_format"
