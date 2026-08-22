from __future__ import annotations

from pathlib import Path

from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner
from notepatch.modules.ai.services.image_naming import (
    normalize_image_remark,
    process_image_remark,
    resolve_image_remark_language,
    schedule_image_remark,
)
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import PNG_BYTES


class ImageRemarkRunner:
    def __init__(self, remark: str = "Biology digestion notes") -> None:
        self.remark = remark
        self.calls: list[dict] = []

    def generate_image_remark(self, workspace_id, document_id, ocr_text, **kwargs):
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "document_id": document_id,
                "ocr_text": ocr_text,
                **kwargs,
            }
        )
        return self.remark


def _image_document(db, *, workspace_id: str, user_id: str, remark_source="original_filename"):
    document = Document(
        workspace_id=workspace_id,
        uploaded_by=user_id,
        title="Independent document title",
        remark="photo.png",
        remark_source=remark_source,
        original_filename="photo.png",
        mime_type="image/png",
        file_size=len(PNG_BYTES),
        file_type="image",
        document_kind="note",
        retention_scope="workspace",
        storage_backend="seaweedfs",
        bucket="notepatch-test",
        object_key=f"workspaces/{workspace_id}/documents/image-doc/original/photo.png",
        status="ready",
        scan_status="skipped",
        metadata_={"image_remark_generation": {"status": "waiting_ocr"}},
    )
    db.add(document)
    db.flush()
    return document


def _ocr_artifact(db, document: Document, fake_storage, text="大肠的结构与消化系统复习笔记"):
    artifact = DocumentArtifact(
        workspace_id=document.workspace_id,
        document_id=document.id,
        artifact_type="ocr_text",
        bucket=document.bucket,
        object_key=f"workspaces/{document.workspace_id}/documents/{document.id}/artifacts/ocr/ocr.txt",
        mime_type="text/plain",
        file_size=len(text.encode()),
        metadata_={"ocr_run_id": "ocr-run-1"},
    )
    db.add(artifact)
    db.flush()
    fake_storage.objects[(artifact.bucket, artifact.object_key)] = {
        "file_size": artifact.file_size,
        "mime_type": "text/plain",
        "metadata": {},
        "body": text.encode(),
    }
    return artifact


def test_image_upload_queues_ocr_before_ai_remark(client, db_sessionmaker, fake_storage, monkeypatch):
    settings = get_settings()
    settings.ai_image_remark_enabled = True
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    user = register_user(client, "image-remark-upload@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "IMG_0250.png",
            "mime_type": "image/png",
            "file_size": len(PNG_BYTES),
            "document_kind": "chat_attachment",
            "save_to_documents": False,
            "client_locale": "pt-BR",
        },
    )
    assert upload.status_code == 201, upload.text
    payload = upload.json()
    assert payload["document"]["remark"] == "IMG_0250.png"
    assert payload["document"]["remark_source"] == "original_filename"
    assert payload["document"]["metadata"]["image_remark_generation"]["client_locale"] == "pt-BR"
    fake_storage.objects[(payload["bucket"], payload["object_key"])] = {
        "file_size": len(PNG_BYTES), "mime_type": "image/png", "metadata": {}, "body": PNG_BYTES,
    }
    completed = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(user["access_token"]),
        json={"upload_session_id": payload["upload_session"]["id"]},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["original_filename"] == "IMG_0250.png"
    assert body["remark"] == "IMG_0250.png"
    assert body["ai_image_naming_status"] == "waiting_ocr"
    assert len(fake_redis.lists["notepatch:tasks:ocr"]) == 1
    with db_sessionmaker() as db:
        task = db.get(Task, fake_redis.lists["notepatch:tasks:ocr"][0])
        assert task.task_type == "ocr_document"
        assert task.payload["options"]["auto_learning"] is False


def test_user_upload_remark_always_wins(client, db_sessionmaker, fake_storage, monkeypatch):
    settings = get_settings()
    settings.ai_image_remark_enabled = True
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    user = register_user(client, "image-user-remark@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "IMG_0250.png", "mime_type": "image/png", "file_size": len(PNG_BYTES),
            "document_kind": "chat_attachment", "save_to_documents": False,
            "remark": "我的消化系统课堂笔记",
        },
    )
    assert upload.status_code == 201, upload.text
    payload = upload.json()
    fake_storage.objects[(payload["bucket"], payload["object_key"])] = {
        "file_size": len(PNG_BYTES), "mime_type": "image/png", "metadata": {}, "body": PNG_BYTES,
    }
    completed = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=auth_headers(user["access_token"]),
        json={"upload_session_id": payload["upload_session"]["id"]},
    )
    assert completed.status_code == 200
    assert completed.json()["remark"] == "我的消化系统课堂笔记"
    assert completed.json()["remark_source"] == "user"
    assert fake_redis.lists == {}


def test_disabled_user_preference_uses_original_filename(client, fake_storage, monkeypatch):
    settings = get_settings()
    settings.ai_image_remark_enabled = True
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    user = register_user(client, "image-remark-disabled@example.com")
    headers = auth_headers(user["access_token"])
    preference = client.patch(
        "/api/v1/auth/preferences",
        headers=headers,
        json={"auto_image_remark_enabled": False},
    )
    assert preference.status_code == 200, preference.text
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=headers,
        json={
            "filename": "plain-name.png", "mime_type": "image/png", "file_size": len(PNG_BYTES),
            "document_kind": "chat_attachment", "save_to_documents": False,
        },
    )
    assert upload.status_code == 201
    payload = upload.json()
    fake_storage.objects[(payload["bucket"], payload["object_key"])] = {
        "file_size": len(PNG_BYTES), "mime_type": "image/png", "metadata": {}, "body": PNG_BYTES,
    }
    completed = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/complete-upload",
        headers=headers,
        json={"upload_session_id": payload["upload_session"]["id"]},
    )
    assert completed.json()["remark"] == "plain-name.png"
    assert completed.json()["remark_source"] == "original_filename"
    assert fake_redis.lists == {}


def test_ai_remark_uses_ocr_text_and_never_changes_original_filename(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    get_settings().ai_image_remark_enabled = True
    user = register_user(client, "image-remark-ocr@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    runner = ImageRemarkRunner("消化系统与大肠结构复习")
    monkeypatch.setattr(
        "notepatch.modules.ai.services.image_naming.OpenClawUserRuntimeService.runtime_for_workspace",
        lambda self, db, workspace_id, model_ids=None: {
            "gateway_url": "http://gateway:18789", "gateway_token": "token",
        },
    )
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    with db_sessionmaker() as db:
        document = _image_document(db, workspace_id=workspace_id, user_id=user["user"]["id"])
        artifact = _ocr_artifact(db, document, fake_storage)
        db.commit()
        task = schedule_image_remark(db, TaskService(db), document, ocr_text_artifact=artifact)
        assert task is not None
        task.status = "running"
        task.attempt = 1
        db.commit()
        process_image_remark(db, TaskService(db), task, fake_storage, runner)
        db.refresh(document)
        assert document.original_filename == "photo.png"
        assert document.title == "Independent document title"
        assert document.remark == "消化系统与大肠结构复习"
        assert document.remark_source == "ai_ocr"
        assert runner.calls[0]["ocr_text"] == "大肠的结构与消化系统复习笔记"
        assert runner.calls[0]["provider_model"] == "openai/gpt-5.6-luna"
        assert runner.calls[0]["output_language"] == "ocr"
        assert task.result["source_variant"] == "ocr_text"


def test_editing_remark_cancels_pending_ai_and_is_workspace_scoped(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    get_settings().ai_image_remark_enabled = True
    user = register_user(client, "image-remark-edit@example.com")
    other = register_user(client, "image-remark-other@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    other_workspace_id = first_workspace_id(client, other["access_token"])
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: fake_redis,
    )
    with db_sessionmaker() as db:
        document = _image_document(db, workspace_id=workspace_id, user_id=user["user"]["id"])
        artifact = _ocr_artifact(db, document, fake_storage)
        db.commit()
        task = schedule_image_remark(db, TaskService(db), document, ocr_text_artifact=artifact)
        document_id = document.id
        task_id = task.id
    response = client.patch(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
        headers=auth_headers(user["access_token"]),
        json={"remark": "用户修改后的备注"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["remark"] == "用户修改后的备注"
    assert response.json()["remark_source"] == "user"
    forbidden = client.patch(
        f"/api/v1/workspaces/{other_workspace_id}/documents/{document_id}",
        headers=auth_headers(other["access_token"]),
        json={"remark": "越权修改"},
    )
    assert forbidden.status_code == 404
    with db_sessionmaker() as db:
        assert db.get(Task, task_id).status == "cancelled"


def test_gateway_remark_uses_ocr_text_fixed_model_and_minimal_reasoning(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "CPU 与寄存器课堂笔记"}}]}

    class Client:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    runner = OpenClawGatewayRunner(client=Client())
    monkeypatch.setattr(runner, "_wait_until_ready", lambda gateway_url: None)
    result = runner.generate_image_remark(
        "workspace-id",
        "document-id",
        "CPU 由运算器和控制器组成，寄存器用于暂存数据",
        original_filename="IMG_0250.jpg",
        runtime={"gateway_url": "http://gateway:18789", "gateway_token": "secret"},
        provider_model="openai/gpt-5.6-luna",
        output_language="en-US",
        max_length=24,
        timeout_seconds=12,
    )
    assert result == "CPU 与寄存器课堂笔记"
    assert captured["json"]["model"] == "openclaw"
    assert captured["json"]["reasoning_effort"] == "minimal"
    assert captured["json"]["reasoning_mode"] == "off"
    assert captured["headers"]["x-openclaw-model"] == "openai/gpt-5.6-luna"
    content = captured["json"]["messages"][1]["content"]
    assert "CPU 由运算器" in content
    assert "2-4 words" in content
    assert "single central topic" in content
    assert "at most 24 characters" in content
    assert "Write the label in English" in content
    assert "image_url" not in str(captured["json"])


def test_image_remark_normalization_keeps_only_a_short_topic_label():
    assert normalize_image_remark(
        "CPU registers, accumulator, and system buses",
        max_length=24,
    ) == "CPU registers"
    assert normalize_image_remark("CPU指令与性能指标概述", max_length=24) == "CPU指令与性能指标"
    assert normalize_image_remark("主题：消化系统复习笔记。", max_length=24) == "消化系统"


def test_image_remark_language_prefers_user_selection_then_client_locale():
    assert resolve_image_remark_language(
        {"ai_preferences": {"answers": {"response_language": "en-US"}}, "client_locale": "pt-BR"}
    ) == "en-US"
    assert resolve_image_remark_language(
        {"ai_preferences": {"answers": {"response_language": "client_locale"}}, "client_locale": "pt-br"}
    ) == "pt-BR"
    assert resolve_image_remark_language(
        {"ai_preferences": {"answers": {"response_language": "client_locale"}}}
    ) == "ocr"
    assert resolve_image_remark_language(
        {"ai_preferences": {"answers": {"response_language": "match_user"}}, "client_locale": "en-US"}
    ) == "ocr"
