from sqlalchemy import select

from notepatch.modules.admin.models.admin import AdminAuditLog, AdminOperation
from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.models.homework import Mistake
from notepatch.modules.learning.models.learning import LearningUnit, StudyNoteVersion
from notepatch.modules.learning.services.note_render import NoteRenderService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.executor import process_task
from notepatch.platform.config import get_settings
from tests.conftest import auth_headers, first_workspace_id, register_user


def _seed_note(db, fake_storage, workspace_id: str) -> tuple[LearningUnit, StudyNoteVersion]:
    unit = LearningUnit(workspace_id=workspace_id, title="Algebra", subject="math", metadata_={})
    db.add(unit)
    db.flush()
    note = StudyNoteVersion(
        workspace_id=workspace_id,
        learning_unit_id=unit.id,
        version_no=1,
        title="Algebra Notes",
        html_object_key=f"workspaces/{workspace_id}/learning-units/{unit.id}/notes/v1/study_note.html",
        json_object_key=f"workspaces/{workspace_id}/learning-units/{unit.id}/notes/v1/study_note.json",
        source_document_ids=["doc-1"],
        source_mistake_ids=[],
        metadata_={"skill": "notepatch_scholar_notes"},
    )
    db.add(note)
    db.commit()
    fake_storage.objects[(fake_storage.bucket, note.html_object_key)] = {
        "file_size": 8,
        "mime_type": "text/html",
        "metadata": {},
        "body": b"<article><h1>Algebra</h1></article>",
    }
    fake_storage.objects[(fake_storage.bucket, note.json_object_key)] = {
        "file_size": 20,
        "mime_type": "application/json",
        "metadata": {},
        "body": {"title": note.title, "html": "<article><h1>Algebra</h1></article>", "outline": []},
    }
    return unit, note


def test_note_revision_creates_version_and_downstream_tasks(client, db_sessionmaker, fake_storage):
    alice = register_user(client, "note-revision@example.com")
    bob = register_user(client, "note-revision-other@example.com")
    workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])
    with db_sessionmaker() as db:
        unit, base = _seed_note(db, fake_storage, workspace_id)
        db.add(
            Mistake(
                workspace_id=workspace_id,
                description="Sign error",
                status="open",
                metadata_={"learning_unit_id": unit.id},
            )
        )
        db.commit()
        unit_id, base_id = unit.id, base.id

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{base_id}/revisions",
        headers=auth_headers(alice["access_token"]),
        json={
            "title": "Edited Algebra",
            "html": (
                '<article><h1>Edited</h1><p><span class="np-font-size-24" '
                'style="font-size:40px">New content</span></p></article>'
            ),
            "edit_summary": "Clarified signs",
        },
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["note"]["version_no"] == 2
    assert payload["note"]["source_version_id"] == base_id
    assert payload["note"]["edit_origin"] == "user"
    assert {item["task_type"] for item in payload["downstream_tasks"]} == {
        "generate_flashcards",
        "highlight_study_notes",
        "purge_study_note_history",
    }
    html_key = payload["note"]["html_object_key"]
    persisted_html = fake_storage.get_text_artifact(html_key)
    assert 'class="np-font-size-24"' in persisted_html
    assert "style=" not in persisted_html
    with db_sessionmaker() as db:
        revision = db.get(StudyNoteVersion, payload["note"]["id"])
        render_url = NoteRenderService().create_url(revision, 900)
    rendered = client.get(render_url)
    assert rendered.status_code == 200
    assert 'class="np-font-size-24"' in rendered.text
    assert "/api/v1/assets/note-themes/" in rendered.text

    stale = client.post(
        f"/api/v1/workspaces/{workspace_id}/learning-units/{unit_id}/notes/{base_id}/revisions",
        headers=auth_headers(alice["access_token"]),
        json={"html": "<p>stale</p>"},
    )
    cross_workspace = client.post(
        f"/api/v1/workspaces/{bob_workspace_id}/learning-units/{unit_id}/notes/{base_id}/revisions",
        headers=auth_headers(bob["access_token"]),
        json={"html": "<p>forbidden</p>"},
    )
    assert stale.status_code == 409
    assert cross_workspace.status_code == 404


def test_admin_user_management_and_forced_password_change(client, db_sessionmaker):
    admin = register_user(client, "management-admin@example.com")
    settings = get_settings()
    settings.admin_emails = "management-admin@example.com"
    created = client.post(
        "/api/v1/admin/users",
        headers=auth_headers(admin["access_token"]),
        json={"email": "managed-user@example.com", "full_name": "Managed User"},
    )
    assert created.status_code == 201, created.text
    temporary_password = created.json()["temporary_password"]
    user_id = created.json()["user"]["id"]
    login = client.post("/api/v1/auth/login", json={"email": "managed-user@example.com", "password": temporary_password})
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is True
    blocked = client.get("/api/v1/workspaces", headers=auth_headers(login.json()["access_token"]))
    assert blocked.status_code == 403
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers(login.json()["access_token"]),
        json={"current_password": temporary_password, "new_password": "new-secure-password"},
    )
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    assert client.get("/api/v1/workspaces", headers=auth_headers(changed.json()["access_token"])).status_code == 200

    disabled = client.patch(
        f"/api/v1/admin/users/{user_id}",
        headers=auth_headers(admin["access_token"]),
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    with db_sessionmaker() as db:
        actions = {item.action for item in db.scalars(select(AdminAuditLog)).all()}
        assert {"user.create", "user.update"} <= actions


def test_admin_user_purge_operation_is_idempotent(client, db_sessionmaker, fake_storage):
    admin = register_user(client, "purge-admin@example.com")
    target = register_user(client, "purge-target@example.com")
    settings = get_settings()
    settings.admin_emails = "purge-admin@example.com"
    target_user_id = target["user"]["id"]
    response = client.delete(
        f"/api/v1/admin/users/{target_user_id}?confirm_email=purge-target%40example.com",
        headers=auth_headers(admin["access_token"]),
    )
    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    task_id = response.json()["task_id"]

    with db_sessionmaker() as db:
        process_task(db, task_id, storage=fake_storage)
        operation = db.get(AdminOperation, operation_id)
        assert operation.phase == "runtime_cleanup_requested"
        operation.phase = "runtime_cleanup_completed"
        db.commit()
        process_task(db, task_id, storage=fake_storage)
        task = db.get(Task, task_id)
        assert task.status == "succeeded", task.error_message
        assert db.get(User, target_user_id) is None
        operation = db.get(AdminOperation, operation_id)
        assert operation.status == "succeeded"
        assert task.status == "succeeded"
