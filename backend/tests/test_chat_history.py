from pathlib import Path

from notepatch.platform.config import get_settings
from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import auth_headers, first_workspace_id, register_user


class RecordingRunner(OpenClawRunner):
    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.payloads: list[dict] = []

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


def create_chat(client, token: str, workspace_id: str, prompt: str, conversation_id: str | None = None) -> dict:
    payload = {"prompt": prompt, "input": {}, "options": {}}
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
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
