from sqlalchemy import delete, select

from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from tests.conftest import auth_headers, first_workspace_id, register_user


def test_register_creates_personal_workspace_and_workspace_list_is_isolated(client):
    alice = register_user(client, "alice@example.com")
    bob = register_user(client, "bob@example.com")

    alice_workspaces = client.get("/api/v1/workspaces", headers=auth_headers(alice["access_token"]))
    bob_workspaces = client.get("/api/v1/workspaces", headers=auth_headers(bob["access_token"]))

    assert alice_workspaces.status_code == 200
    assert bob_workspaces.status_code == 200
    assert len(alice_workspaces.json()) == 1
    assert len(bob_workspaces.json()) == 1
    assert alice_workspaces.json()[0]["id"] != bob_workspaces.json()[0]["id"]
    assert alice_workspaces.json()[0]["type"] == "personal"


def test_non_member_cannot_access_other_workspace(client):
    alice = register_user(client, "alice2@example.com")
    bob = register_user(client, "bob2@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])

    response = client.get(f"/api/v1/workspaces/{alice_workspace_id}", headers=auth_headers(bob["access_token"]))

    assert response.status_code == 403


def test_personal_workspace_cannot_be_created_twice(client):
    alice = register_user(client, "alice-create-workspace@example.com")

    response = client.post(
        "/api/v1/workspaces",
        headers=auth_headers(alice["access_token"]),
        json={"name": "Alice Family", "type": "family"},
    )
    workspaces = client.get("/api/v1/workspaces", headers=auth_headers(alice["access_token"]))

    assert response.status_code == 409
    assert response.json()["detail"] == "Personal workspace already exists"
    assert workspaces.status_code == 200
    assert len(workspaces.json()) == 1
    assert workspaces.json()[0]["type"] == "personal"


def test_create_workspace_recovers_missing_personal_workspace_and_ignores_type(client, db_sessionmaker):
    alice = register_user(client, "alice-recover-workspace@example.com")
    current_user = client.get("/api/v1/auth/me", headers=auth_headers(alice["access_token"])).json()

    with db_sessionmaker() as db:
        workspace_ids = db.scalars(
            select(Workspace.id).where(Workspace.owner_user_id == current_user["id"])
        ).all()
        db.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids)))
        db.execute(delete(Workspace).where(Workspace.owner_user_id == current_user["id"]))
        db.commit()

    response = client.post(
        "/api/v1/workspaces",
        headers=auth_headers(alice["access_token"]),
        json={"name": "Recovered Workspace", "type": "school"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["name"] == "Recovered Workspace"
    assert response.json()["type"] == "personal"


def test_workspace_members_are_disabled_for_personal_workspaces(client):
    alice = register_user(client, "alice-members-disabled@example.com")
    bob = register_user(client, "bob-members-disabled@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])

    response = client.post(
        f"/api/v1/workspaces/{alice_workspace_id}/members",
        headers=auth_headers(alice["access_token"]),
        json={"email": bob["user"]["email"], "role_name": "student"},
    )

    assert response.status_code == 410
    assert response.json()["detail"] == "Workspace members are disabled for personal workspaces"
