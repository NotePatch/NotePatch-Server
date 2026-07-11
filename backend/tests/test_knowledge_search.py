from notepatch.modules.learning.api.knowledge import get_knowledge_service
from notepatch.entrypoints.api import app
from tests.conftest import auth_headers, first_workspace_id, register_user


class RecordingKnowledgeService:
    def __init__(self) -> None:
        self.workspace_ids = []

    def search(self, **kwargs):
        self.workspace_ids.append(kwargs["workspace_id"])
        return []


def test_knowledge_search_is_workspace_scoped(client):
    alice = register_user(client, "knowledge-a@example.com")
    bob = register_user(client, "knowledge-b@example.com")
    alice_workspace = first_workspace_id(client, alice["access_token"])
    service = RecordingKnowledgeService()
    app.dependency_overrides[get_knowledge_service] = lambda: service
    try:
        allowed = client.post(
            f"/api/v1/workspaces/{alice_workspace}/knowledge/search",
            headers=auth_headers(alice["access_token"]),
            json={"query": "linear function"},
        )
        assert allowed.status_code == 200
        denied = client.post(
            f"/api/v1/workspaces/{alice_workspace}/knowledge/search",
            headers=auth_headers(bob["access_token"]),
            json={"query": "linear function"},
        )
        assert denied.status_code == 403
        assert service.workspace_ids == [alice_workspace]
    finally:
        app.dependency_overrides.pop(get_knowledge_service, None)
