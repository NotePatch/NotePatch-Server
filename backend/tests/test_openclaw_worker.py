from pathlib import Path

from notepatch.platform.config import get_settings
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.ai.services.gateway import OpenClawRunner
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import auth_headers, first_workspace_id, register_user


class FakeGatewayRunner(OpenClawRunner):
    def __init__(self, root: Path) -> None:
        self.root = root

    def prepare_task_dir(self, workspace_id: str, task_id: str) -> Path:
        task_dir = self.root / workspace_id / task_id
        (task_dir / "input").mkdir(parents=True, exist_ok=True)
        (task_dir / "output").mkdir(parents=True, exist_ok=True)
        return task_dir

    def run_task(self, workspace_id: str, task_id: str, payload: dict) -> dict:
        task_dir = self.prepare_task_dir(workspace_id, task_id)
        (task_dir / "output" / "result.json").write_text('{"answer":"gateway ok"}', encoding="utf-8")
        return {"runner": "gateway", "answer": "gateway ok", "response": {"choices": []}}

    def collect_output(self, workspace_id: str, task_id: str) -> dict:
        task_dir = self.prepare_task_dir(workspace_id, task_id)
        return {"output_dir": str(task_dir / "output"), "files": ["result.json"]}

    def cleanup(self, workspace_id: str, task_id: str) -> None:
        return None


class FailingGatewayRunner(FakeGatewayRunner):
    def run_task(self, workspace_id: str, task_id: str, payload: dict) -> dict:
        raise RuntimeError("gateway unavailable")


def create_openclaw_chat(client, token: str, workspace_id: str) -> str:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json={
            "prompt": "请总结这份作业",
            "input": {"document_id": "doc-1"},
            "options": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_upload_session(client, token: str, workspace_id: str) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": "missing.pdf",
            "mime_type": "application/pdf",
            "file_size": 12,
            "document_kind": "homework",
            "title": "missing.pdf",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_openclaw_worker_uses_injected_gateway_runner(client, db_sessionmaker, fake_storage, tmp_path):
    user = register_user(client, "openclaw-worker@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    task_id = create_openclaw_chat(client, user["access_token"], workspace_id)

    with db_sessionmaker() as db:
        task = process_task(
            db,
            task_id,
            storage=fake_storage,
            openclaw_runner=FakeGatewayRunner(tmp_path),
        )
        assert task is not None
        assert task.status == "succeeded"
        assert task.result["runner"] == "gateway"
        assert task.result["answer"] == "gateway ok"
        assert task.result["output_key"] in {key[1] for key in fake_storage.objects}
        events = db.query(TaskEvent).filter_by(task_id=task_id).all()
        assert {event.event_type for event in events} >= {"openclaw_prepare", "openclaw_run", "succeeded"}


def test_openclaw_worker_marks_task_failed_when_gateway_runner_fails(client, db_sessionmaker, fake_storage, tmp_path):
    user = register_user(client, "openclaw-failure@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    task_id = create_openclaw_chat(client, user["access_token"], workspace_id)

    with db_sessionmaker() as db:
        task = process_task(
            db,
            task_id,
            storage=fake_storage,
            openclaw_runner=FailingGatewayRunner(tmp_path),
        )
        assert task is not None
        assert task.status == "failed"
        assert "gateway unavailable" in (task.error_message or "")
        assert db.get(Task, task_id).status == "failed"


def test_openclaw_worker_fails_fast_without_openai_key(client, db_sessionmaker, fake_storage):
    settings = get_settings()
    old_key = settings.openai_api_key
    settings.openai_api_key = None
    user = register_user(client, "openclaw-missing-key@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    task_id = create_openclaw_chat(client, user["access_token"], workspace_id)

    try:
        with db_sessionmaker() as db:
            task = process_task(db, task_id, storage=fake_storage)
            assert task is not None
            assert task.status == "failed"
            assert "OPENAI_API_KEY" in (task.error_message or "")
            events = db.query(TaskEvent).filter_by(task_id=task_id, event_type="openclaw_prepare").all()
            assert events
            assert events[0].data["model"] == settings.openclaw_gateway_model
            assert events[0].data["gateway_container"].startswith("notepatch-openclaw-")
    finally:
        settings.openai_api_key = old_key


def test_openclaw_worker_skips_unfinished_document_mirror(client, db_sessionmaker, fake_storage, tmp_path):
    user = register_user(client, "openclaw-storage-failure@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = create_upload_session(client, user["access_token"], workspace_id)
    task_id = create_openclaw_chat(client, user["access_token"], workspace_id)

    with db_sessionmaker() as db:
        task = process_task(
            db,
            task_id,
            storage=fake_storage,
            openclaw_runner=FakeGatewayRunner(tmp_path),
        )
        assert task is not None
        assert task.status == "succeeded"
        events = db.query(TaskEvent).filter_by(task_id=task_id, event_type="openclaw_prepare").all()
        assert events
        assert events[0].data["documents_skipped"] == 1
        assert events[0].data["skipped_documents"][0]["id"] == upload["document"]["id"]
        assert events[0].data["skipped_documents"][0]["reason"] == "status_not_mirrorable"


def test_chat_creates_openclaw_task_and_legacy_routes_are_removed(client):
    user = register_user(client, "openclaw-chat-api@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(user["access_token"]),
        json={"prompt": "帮我复习今天的内容", "input": {"topic": "math"}, "options": {"model": "openclaw"}},
    )

    assert response.status_code == 201, response.text
    task = response.json()
    assert task["task_type"] == "openclaw_agent_run"
    assert task["payload"]["prompt"] == "帮我复习今天的内容"
    assert task["payload"]["input"] == {"topic": "math"}
    assert task["payload"]["options"] == {"model": "openclaw"}
    assert task["payload"]["conversation_id"]
    assert task["payload"]["user_message_id"]
    assert task["payload"]["assistant_message_id"]

    old_route = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/openclaw-task",
        headers=auth_headers(user["access_token"]),
        json={"prompt": "legacy"},
    )
    assert old_route.status_code == 404

    old_payload = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(user["access_token"]),
        json={"message": "legacy", "subject": "math"},
    )
    assert old_payload.status_code == 422
