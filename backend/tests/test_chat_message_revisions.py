from notepatch.modules.ai.models.chat import ChatMessage
from notepatch.modules.tasks.models.task import Task
from tests.conftest import auth_headers, first_workspace_id, register_user


def _chat(client, token: str, workspace_id: str, prompt: str, conversation_id: str | None = None):
    payload = {"prompt": prompt, "input": {}, "options": {}}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(token),
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_revise_message_supersedes_tail_and_creates_new_task(client, db_sessionmaker):
    registered = register_user(client, "revision-owner@example.com")
    token = registered["access_token"]
    workspace_id = first_workspace_id(client, token)
    first = _chat(client, token, workspace_id, "first prompt")
    conversation_id = first["payload"]["conversation_id"]
    second = _chat(client, token, workspace_id, "second prompt", conversation_id)

    before = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages",
        headers=auth_headers(token),
    ).json()["items"]
    first_user_message = next(message for message in before if message["role"] == "user")
    revised = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages/{first_user_message['id']}/revisions",
        headers=auth_headers(token),
        json={"prompt": "corrected first prompt", "options": {"temperature": 0.4}},
    )
    assert revised.status_code == 201, revised.text
    task = revised.json()["data"]
    assert task["task_type"] == "openclaw_agent_run"
    assert task["payload"]["revised_message_id"] == first_user_message["id"]
    assert task["payload"]["options"]["temperature"] == 0.4

    current = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages",
        headers=auth_headers(token),
    ).json()["items"]
    assert [item["role"] for item in current] == ["user", "assistant"]
    assert current[0]["content"] == "corrected first prompt"
    assert current[0]["revision_of_message_id"] == first_user_message["id"]

    audit_view = client.get(
        f"/api/v1/workspaces/{workspace_id}/ai/conversations/{conversation_id}/messages?include_superseded=true",
        headers=auth_headers(token),
    ).json()["items"]
    assert len(audit_view) == 6
    assert sum(item["superseded_at"] is not None for item in audit_view) == 4

    with db_sessionmaker() as db:
        old_tasks = db.query(Task).filter(Task.id.in_([first["id"], second["id"]])).all()
        assert all(task.status == "cancelled" for task in old_tasks)
        active_messages = db.query(ChatMessage).filter(ChatMessage.superseded_at.is_(None)).all()
        assert len(active_messages) == 2


def test_revise_message_is_workspace_isolated(client):
    owner = register_user(client, "revision-a@example.com")
    attacker = register_user(client, "revision-b@example.com")
    owner_workspace = first_workspace_id(client, owner["access_token"])
    attacker_workspace = first_workspace_id(client, attacker["access_token"])
    task = _chat(client, owner["access_token"], owner_workspace, "private message")
    conversation_id = task["payload"]["conversation_id"]
    messages = client.get(
        f"/api/v1/workspaces/{owner_workspace}/ai/conversations/{conversation_id}/messages",
        headers=auth_headers(owner["access_token"]),
    ).json()["items"]
    message_id = next(item["id"] for item in messages if item["role"] == "user")
    response = client.post(
        f"/api/v1/workspaces/{attacker_workspace}/ai/conversations/{conversation_id}/messages/{message_id}/revisions",
        headers=auth_headers(attacker["access_token"]),
        json={"prompt": "stolen"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "chat_message_not_found"


def test_temperature_validation_is_frozen_in_chat_task(client):
    registered = register_user(client, "temperature-chat@example.com")
    workspace_id = first_workspace_id(client, registered["access_token"])
    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(registered["access_token"]),
        json={"prompt": "creative", "options": {"temperature": 1.25}},
    )
    assert accepted.status_code == 201
    assert accepted.json()["payload"]["options"]["temperature"] == 1.25
    rejected = client.post(
        f"/api/v1/workspaces/{workspace_id}/ai/chat",
        headers=auth_headers(registered["access_token"]),
        json={"prompt": "too hot", "options": {"temperature": 2.1}},
    )
    assert rejected.status_code == 422
