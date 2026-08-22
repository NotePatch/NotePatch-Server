from pathlib import Path

from notepatch.modules.documents.models.document import Document
from notepatch.platform.config import get_settings
from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.tasks.models.task import TaskEvent
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import auth_headers, first_workspace_id, register_user


class RecordingRunner(OpenClawRunner):
    def __init__(
        self,
        root: Path,
        *,
        fail: bool = False,
        generated_title: str | None = None,
        title_error: bool = False,
    ) -> None:
        self.root = root
        self.fail = fail
        self.generated_title = generated_title
        self.title_error = title_error
        self.payloads: list[dict] = []
        self.title_calls: list[dict] = []

    def prepare_task_dir(self, workspace_id: str, task_id: str) -> Path:
        task_dir = self.root / workspace_id / task_id
        (task_dir / "output").mkdir(parents=True, exist_ok=True)
        return task_dir

    def run_task(self, workspace_id: str, task_id: str, payload: dict) -> dict:
        self.payloads.append(payload)
        if self.fail:
            raise RuntimeError("gateway unavailable")
        return {"runner": "gateway", "answer": f"answer: {payload['prompt']}"}

    def collect_output(self, workspace_id: str, task_id: str) -> dict:
        return {"output_dir": str(self.prepare_task_dir(workspace_id, task_id) / "output"), "files": []}

    def cleanup(self, workspace_id: str, task_id: str) -> None:
        return None

    def generate_conversation_title(
        self,
        workspace_id: str,
        conversation_id: str,
        messages: list[dict[str, str]],
        **kwargs,
    ) -> str | None:
        self.title_calls.append(
            {
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "messages": messages,
                **kwargs,
            }
        )
        if self.title_error:
            raise RuntimeError("title gateway unavailable")
        return self.generated_title


def test_generated_title_truncates_latin_text_at_word_boundary():
    title = ChatService._normalize_generated_title(
        "Atmospheric scattering and the blue daytime sky",
        max_length=30,
    )
    assert title == "Atmospheric scattering and the"


def create_chat(
    client,
    token: str,
    workspace_id: str,
    prompt: str,
    conversation_id: str | None = None,
    *,
    client_locale: str | None = None,
    accept_language: str | None = None,
) -> dict:
    payload = {"prompt": prompt, "input": {}, "options": {}}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    if client_locale is not None:
        payload["client_locale"] = client_locale
    headers = auth_headers(token)
    if accept_language is not None:
        headers["Accept-Language"] = accept_language
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=headers,
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def messages(client, token: str, workspace_id: str, conversation_id: str) -> list[dict]:
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages",
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_chat_initial_greeting_is_localized_and_not_persisted(client):
    user = register_user(client, "chat-greeting@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/greeting?client_locale=pt-BR",
        headers=auth_headers(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "assistant_name": "NotePatch AI",
        "message": (
            "A NotePatch AI pode organizar ideias, analisar materiais de estudo e responder "
            "a perguntas. As respostas aceitam Markdown."
        ),
        "message_key": "ai.chat.initial_greeting",
        "format": "markdown",
        "locale": "pt-BR",
        "onboarding_required": False,
        "onboarding_version": 1,
        "questions": body["questions"],
    }
    assert len(body["questions"]) == 7
    assert {item["id"] for item in body["questions"]} == {
        "response_language",
        "collaboration_style",
        "response_depth",
        "response_structure",
        "clarification_policy",
        "feedback_tone",
        "learning_guidance",
    }

    conversations = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations",
        headers=auth_headers(token),
    )
    assert conversations.status_code == 200
    assert conversations.json()["total"] == 0

def create_ready_image(
    client,
    db_sessionmaker,
    fake_storage,
    token: str,
    workspace_id: str,
    filename: str,
    *,
    save_to_documents: bool = True,
) -> dict:
    body = b"test image bytes"
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "image/jpeg",
            "file_size": len(body),
            "document_kind": "chat_attachment",
            "save_to_documents": save_to_documents,
            "title": filename,
        },
    )
    assert response.status_code == 201, response.text
    upload = response.json()
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": len(body),
        "mime_type": "image/jpeg",
        "metadata": {},
        "body": body,
    }
    with db_sessionmaker() as db:
        document = db.get(Document, upload["document"]["id"])
        document.status = "ready"
        db.commit()
    return upload


def test_conversation_only_attachment_is_hidden_bound_and_purged_with_conversation(
    client,
    db_sessionmaker,
    fake_storage,
    tmp_path,
):
    user = register_user(client, "chat-ephemeral-attachment@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = create_ready_image(
        client,
        db_sessionmaker,
        fake_storage,
        token,
        workspace_id,
        "temporary-photo.jpg",
        save_to_documents=False,
    )
    document_id = upload["document"]["id"]
    assert upload["document"]["retention_scope"] == "conversation"
    assert upload["document"]["save_to_documents"] is False

    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=auth_headers(token),
    )
    assert listed.status_code == 200
    assert all(item["id"] != document_id for item in listed.json())

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={
            "prompt": "这张临时图片是什么？",
            "input": {"attachments": [{"document_id": document_id}]},
            "options": {},
        },
    )
    assert response.status_code == 201, response.text
    task = response.json()
    conversation_id = task["payload"]["conversation_id"]
    with db_sessionmaker() as db:
        document = db.get(Document, document_id)
        assert document.chat_conversation_id == conversation_id

    runner = RecordingRunner(tmp_path)
    with db_sessionmaker() as db:
        process_task(db, task["id"], storage=fake_storage, openclaw_runner=runner)
    attachment = runner.payloads[-1]["input"]["attachments"][0]
    assert attachment["availability"] == "available"
    assert attachment["save_to_documents"] is False

    other_conversation = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={
            "prompt": "跨会话读取",
            "input": {"attachments": [{"document_id": document_id}]},
            "options": {},
        },
    )
    assert other_conversation.status_code == 404

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 204
    with db_sessionmaker() as db:
        document = db.get(Document, document_id)
        assert document.status == "deleted"
        assert document.purge_status == "queued"
        assert document.purge_task_id is not None


def test_saved_chat_attachment_remains_in_documents_after_conversation_delete(
    client,
    db_sessionmaker,
    fake_storage,
):
    user = register_user(client, "chat-saved-attachment@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = create_ready_image(
        client,
        db_sessionmaker,
        fake_storage,
        token,
        workspace_id,
        "saved-photo.jpg",
        save_to_documents=True,
    )
    document_id = upload["document"]["id"]
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={
            "prompt": "保存这张图片",
            "input": {"attachments": [{"document_id": document_id}]},
            "options": {},
        },
    )
    assert response.status_code == 201
    conversation_id = response.json()["payload"]["conversation_id"]
    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    )
    assert deleted.status_code == 204

    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/documents",
        headers=auth_headers(token),
    )
    assert {item["id"] for item in listed.json()} == {document_id}
    with db_sessionmaker() as db:
        document = db.get(Document, document_id)
        assert document.status == "ready"
        assert document.chat_conversation_id is None


def test_non_chat_upload_cannot_use_conversation_only_retention(client):
    user = register_user(client, "invalid-ephemeral-upload@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(user["access_token"]),
        json={
            "filename": "hidden-homework.pdf",
            "mime_type": "application/pdf",
            "file_size": 10,
            "document_kind": "homework",
            "save_to_documents": False,
        },
    )
    assert response.status_code == 422


def test_chat_creates_history_and_worker_completes_assistant_message(client, db_sessionmaker, fake_storage, tmp_path):
    user = register_user(client, "chat-history@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    task = create_chat(client, token, workspace_id, "请解释二次函数")
    conversation_id = task["payload"]["conversation_id"]

    listed = client.get(f"/api/v1/workspaces/{workspace_id}/ai/conversations", headers=auth_headers(token))
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["title"] == "请解释二次函数"

    initial_messages = messages(client, token, workspace_id, conversation_id)
    assert [(item["role"], item["status"]) for item in initial_messages] == [
        ("user", "succeeded"),
        ("assistant", "queued"),
    ]
    assert initial_messages[1]["task_id"] == task["id"]

    runner = RecordingRunner(tmp_path)
    with db_sessionmaker() as db:
        completed = process_task(db, task["id"], storage=fake_storage, openclaw_runner=runner)
        assert completed is not None
        assert completed.status == "succeeded"
        assert completed.result["answer"] == "answer: 请解释二次函数"

    completed_messages = messages(client, token, workspace_id, conversation_id)
    assert completed_messages[1]["status"] == "succeeded"
    assert completed_messages[1]["content"] == "answer: 请解释二次函数"


def test_openclaw_generates_conversation_title_from_early_messages(
    client,
    db_sessionmaker,
    fake_storage,
    tmp_path,
):
    user = register_user(client, "chat-auto-title@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    task = create_chat(
        client,
        token,
        workspace_id,
        "请详细解释二次函数的图像和顶点公式",
        client_locale="pt-BR",
    )
    conversation_id = task["payload"]["conversation_id"]
    runner = RecordingRunner(tmp_path, generated_title='"二次函数图像与顶点"')

    with db_sessionmaker() as db:
        completed = process_task(db, task["id"], storage=fake_storage, openclaw_runner=runner)
        assert completed is not None
        assert completed.status == "succeeded"
        events = db.query(TaskEvent).filter_by(task_id=task["id"]).all()
        assert "chat_title_generated" in {event.event_type for event in events}
        event_order = {event.event_type: event.sequence_no for event in events}
        assert event_order["chat_title_generated"] < event_order["succeeded"]

    conversation = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    )
    assert conversation.status_code == 200
    assert conversation.json()["title"] == "二次函数图像与顶点"
    assert conversation.json()["title_source"] == "ai"
    assert conversation.json()["title_generated_at"] is not None
    assert runner.title_calls[0]["messages"] == [
        {"role": "user", "content": "请详细解释二次函数的图像和顶点公式"},
        {"role": "assistant", "content": "answer: 请详细解释二次函数的图像和顶点公式"},
    ]
    assert runner.title_calls[0]["provider_model"] == "openai/gpt-5.4-mini"
    assert runner.title_calls[0]["client_locale"] == "pt-BR"


def test_chat_snapshots_locale_from_accept_language(client):
    user = register_user(client, "chat-accept-language@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)

    task = create_chat(
        client,
        token,
        workspace_id,
        "oi",
        accept_language="pt-BR,pt;q=0.9,en;q=0.5",
    )

    assert task["payload"]["client_locale"] == "pt-BR"


def test_chat_explicit_locale_wins_and_invalid_locale_is_rejected(client):
    user = register_user(client, "chat-explicit-locale@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)

    task = create_chat(
        client,
        token,
        workspace_id,
        "hello",
        client_locale="en-us",
        accept_language="zh-CN",
    )
    assert task["payload"]["client_locale"] == "en-US"

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={"prompt": "hello", "client_locale": "../../en", "input": {}, "options": {}},
    )
    assert response.status_code == 422


def test_manual_conversation_title_is_not_overwritten_by_openclaw(
    client,
    db_sessionmaker,
    fake_storage,
    tmp_path,
):
    user = register_user(client, "chat-manual-title@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    task = create_chat(client, token, workspace_id, "一个很长的初始问题")
    conversation_id = task["payload"]["conversation_id"]
    renamed = client.patch(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
        json={"title": "我的复习计划"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title_source"] == "manual"
    runner = RecordingRunner(tmp_path, generated_title="不应采用的标题")

    with db_sessionmaker() as db:
        completed = process_task(db, task["id"], storage=fake_storage, openclaw_runner=runner)
        assert completed is not None
        assert completed.status == "succeeded"

    conversation = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    ).json()
    assert conversation["title"] == "我的复习计划"
    assert conversation["title_source"] == "manual"
    assert runner.title_calls == []


def test_title_generation_failure_keeps_answer_and_prompt_title(
    client,
    db_sessionmaker,
    fake_storage,
    tmp_path,
):
    user = register_user(client, "chat-title-failure@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    task = create_chat(client, token, workspace_id, "保留这个临时标题")
    conversation_id = task["payload"]["conversation_id"]

    with db_sessionmaker() as db:
        completed = process_task(
            db,
            task["id"],
            storage=fake_storage,
            openclaw_runner=RecordingRunner(tmp_path, title_error=True),
        )
        assert completed is not None
        assert completed.status == "succeeded"
        events = db.query(TaskEvent).filter_by(task_id=task["id"]).all()
        assert "chat_title_generation_failed" in {event.event_type for event in events}

    conversation = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(token),
    ).json()
    assert conversation["title"] == "保留这个临时标题"
    assert conversation["title_source"] == "prompt"
    assistant = messages(client, token, workspace_id, conversation_id)[1]
    assert assistant["status"] == "succeeded"


def test_chat_history_toggle_controls_openclaw_context_without_deleting_messages(
    client, db_sessionmaker, fake_storage, tmp_path
):
    user = register_user(client, "chat-history-toggle@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    runner = RecordingRunner(tmp_path)

    first = create_chat(client, token, workspace_id, "第一条问题")
    conversation_id = first["payload"]["conversation_id"]
    with db_sessionmaker() as db:
        process_task(db, first["id"], storage=fake_storage, openclaw_runner=runner)

    second = create_chat(client, token, workspace_id, "第二条问题", conversation_id)
    with db_sessionmaker() as db:
        process_task(db, second["id"], storage=fake_storage, openclaw_runner=runner)
    assert runner.payloads[-1]["conversation_messages"] == [
        {"role": "user", "content": "第一条问题"},
        {"role": "assistant", "content": "answer: 第一条问题"},
        {"role": "user", "content": "第二条问题"},
    ]

    preferences = client.patch(
        "/api/v1/auth/preferences",
        headers=auth_headers(token),
        json={"ai_history_enabled": False},
    )
    assert preferences.status_code == 200
    assert preferences.json()["ai_history_enabled"] is False
    assert client.get("/api/v1/auth/me", headers=auth_headers(token)).json()["ai_history_enabled"] is False

    third = create_chat(client, token, workspace_id, "第三条问题", conversation_id)
    assert third["payload"]["ai_history_enabled"] is False
    # Preference changes after submission affect only future tasks, not this queued task.
    enabled = client.patch(
        "/api/v1/auth/preferences",
        headers=auth_headers(token),
        json={"ai_history_enabled": True},
    )
    assert enabled.status_code == 200
    with db_sessionmaker() as db:
        process_task(db, third["id"], storage=fake_storage, openclaw_runner=runner)
    assert runner.payloads[-1]["conversation_messages"] == []
    assert len(messages(client, token, workspace_id, conversation_id)) == 6


def test_conversation_management_is_scoped_and_soft_deleted(client):
    alice = register_user(client, "chat-alice@example.com")
    bob = register_user(client, "chat-bob@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    task = create_chat(client, alice["access_token"], alice_workspace_id, "Alice 的对话")
    conversation_id = task["payload"]["conversation_id"]

    other_read = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(bob["access_token"]),
    )
    assert other_read.status_code == 404
    other_send = client.post(
        f"/api/v1/workspaces/{bob_workspace_id}/ai/chat",
        headers=auth_headers(bob["access_token"]),
        json={"prompt": "尝试访问", "conversation_id": conversation_id},
    )
    assert other_send.status_code == 404

    rename = client.patch(
        f"/api/v1/workspaces/{alice_workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(alice["access_token"]),
        json={"title": "数学复习"},
    )
    assert rename.status_code == 200
    assert rename.json()["title"] == "数学复习"

    deleted = client.delete(
        f"/api/v1/workspaces/{alice_workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(alice["access_token"]),
    )
    assert deleted.status_code == 204
    assert client.get(
        f"/api/v1/workspaces/{alice_workspace_id}/ai/conversations/{conversation_id}",
        headers=auth_headers(alice["access_token"]),
    ).status_code == 404
    assert client.post(
        f"/api/v1/workspaces/{alice_workspace_id}/ai/chat",
        headers=auth_headers(alice["access_token"]),
        json={"prompt": "已删除会话", "conversation_id": conversation_id},
    ).status_code == 404


def test_failed_openclaw_task_marks_assistant_message_failed(client, db_sessionmaker, fake_storage, tmp_path):
    user = register_user(client, "chat-failure@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    task = create_chat(client, token, workspace_id, "会失败的问题")
    conversation_id = task["payload"]["conversation_id"]

    with db_sessionmaker() as db:
        failed = process_task(
            db,
            task["id"],
            storage=fake_storage,
            openclaw_runner=RecordingRunner(tmp_path, fail=True),
        )
        assert failed is not None
        assert failed.status == "failed"

    assistant = messages(client, token, workspace_id, conversation_id)[1]
    assert assistant["status"] == "failed"
    assert "gateway unavailable" in (assistant["error_message"] or "")


def test_chat_history_window_is_limited(client, db_sessionmaker, fake_storage, tmp_path):
    settings = get_settings()
    old_limit = settings.ai_chat_history_message_limit
    settings.ai_chat_history_message_limit = 2
    try:
        user = register_user(client, "chat-history-window@example.com")
        token = user["access_token"]
        workspace_id = first_workspace_id(client, token)
        runner = RecordingRunner(tmp_path)
        first = create_chat(client, token, workspace_id, "问题一")
        conversation_id = first["payload"]["conversation_id"]
        with db_sessionmaker() as db:
            process_task(db, first["id"], storage=fake_storage, openclaw_runner=runner)
        second = create_chat(client, token, workspace_id, "问题二", conversation_id)
        with db_sessionmaker() as db:
            process_task(db, second["id"], storage=fake_storage, openclaw_runner=runner)
        third = create_chat(client, token, workspace_id, "问题三", conversation_id)
        with db_sessionmaker() as db:
            process_task(db, third["id"], storage=fake_storage, openclaw_runner=runner)

        assert runner.payloads[-1]["conversation_messages"] == [
            {"role": "assistant", "content": "answer: 问题二"},
            {"role": "user", "content": "问题三"},
        ]
    finally:
        settings.ai_chat_history_message_limit = old_limit

def test_chat_attachments_persist_and_rebind_to_each_task_snapshot(
    client,
    db_sessionmaker,
    fake_storage,
    tmp_path,
):
    user = register_user(client, "chat-attachments@example.com")
    token = user["access_token"]
    workspace_id = first_workspace_id(client, token)
    upload = create_ready_image(
        client,
        db_sessionmaker,
        fake_storage,
        token,
        workspace_id,
        "lesson-photo.jpg",
    )
    document_id = upload["document"]["id"]

    first_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={
            "prompt": "这张图里是什么？",
            "input": {
                "attachments": [
                    {
                        "document_id": document_id,
                        "filename": "../../spoofed.jpg",
                        "mime_type": "text/plain",
                    }
                ]
            },
            "options": {},
        },
    )
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()
    conversation_id = first["payload"]["conversation_id"]
    stored_attachment = messages(client, token, workspace_id, conversation_id)[0]["attachments"][0]
    assert stored_attachment["document_id"] == document_id
    assert stored_attachment["filename"] == "lesson-photo.jpg"
    assert stored_attachment["mime_type"] == "image/jpeg"

    runner = RecordingRunner(tmp_path)
    with db_sessionmaker() as db:
        process_task(db, first["id"], storage=fake_storage, openclaw_runner=runner)
    current_attachment = runner.payloads[-1]["input"]["attachments"][0]
    assert current_attachment["availability"] == "available"
    assert first["id"] in current_attachment["original_path"]
    assert current_attachment["original_path"].endswith("/original/lesson-photo.jpg")

    second = create_chat(client, token, workspace_id, "继续解释这张图", conversation_id)
    with db_sessionmaker() as db:
        process_task(db, second["id"], storage=fake_storage, openclaw_runner=runner)

    historical_user_message = runner.payloads[-1]["conversation_messages"][0]["content"]
    assert "NotePatch attachments referenced by this message:" in historical_user_message
    assert document_id in historical_user_message
    assert second["id"] in historical_user_message
    assert first["id"] not in historical_user_message
    persisted = messages(client, token, workspace_id, conversation_id)
    assert persisted[0]["attachments"][0]["document_id"] == document_id


def test_chat_attachment_document_is_strictly_workspace_scoped(
    client,
    db_sessionmaker,
    fake_storage,
):
    alice = register_user(client, "chat-attachment-alice@example.com")
    bob = register_user(client, "chat-attachment-bob@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    upload = create_ready_image(
        client,
        db_sessionmaker,
        fake_storage,
        alice["access_token"],
        alice_workspace_id,
        "alice-private.jpg",
    )

    response = client.post(
        f"/api/v1/workspaces/{bob_workspace_id}/ai/chat",
        headers=auth_headers(bob["access_token"]),
        json={
            "prompt": "读取这张图",
            "input": {"attachments": [{"document_id": upload["document"]["id"]}]},
            "options": {},
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Attachment document not found"
