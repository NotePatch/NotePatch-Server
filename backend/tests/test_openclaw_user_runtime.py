import json
import uuid
from pathlib import Path

import pytest

from notepatch.platform.config import get_settings
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError, OpenClawUserRuntimeService
from tests.conftest import auth_headers, first_workspace_id, register_user


class BrokenStorage:
    bucket = "notepatch-test"

    def download_file(self, bucket: str, object_key: str, dest_path):
        raise RuntimeError("storage service exploded")

    @staticmethod
    def is_object_not_found_error(exc: Exception) -> bool:
        return False


def test_register_generates_openclaw_user_runtime(client):
    user = register_user(client, "openclaw-runtime@example.com")
    user_id = user["user"]["id"]
    service = OpenClawUserRuntimeService()
    root = Path(get_settings().openclaw_user_runtime_root) / "users" / user_id

    assert (root / "home" / ".openclaw" / "openclaw.json").exists()
    assert (root / "home" / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json").exists()
    expected_skills = {
        "notepatch_question_extractor",
        "notepatch_kb_builder",
        "notepatch_scholar_notes",
        "notepatch_grading",
        "notepatch_note_highlighter",
        "notepatch_flashcards",
    }
    for skill in expected_skills:
        skill_path = root / "workspace" / "skills" / skill / "SKILL.md"
        assert skill_path.exists()
        assert skill_path.read_text(encoding="utf-8").startswith("---\nname:")
        assert not (root / "home" / ".openclaw" / "skills" / skill).exists()
    assert (root / "notepatch-runtime.json").exists()
    assert not (root / "home" / ".openclaw" / "notepatch-runtime.json").exists()
    assert (root / "workspace" / "notepatch" / "documents").is_dir()
    assert (root / "workspace" / "notepatch" / "openclaw" / "tasks").is_dir()
    assert (root / "docker-compose.yml").exists()
    assert (root / ".env").exists()

    config = json.loads((root / "home" / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    auth_profiles = json.loads(
        (root / "home" / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = json.loads((root / "notepatch-runtime.json").read_text(encoding="utf-8"))
    assert "identity" not in config
    assert config["agents"]["defaults"]["workspace"] == str(service.workspace_dir(user_id))
    assert config["agents"]["defaults"]["model"] == {"primary": get_settings().openclaw_agent_model}
    assert config["agents"]["defaults"]["sandbox"]["mode"] == "all"
    assert config["agents"]["defaults"]["sandbox"]["workspaceAccess"] == "rw"
    assert set(config["agents"]["defaults"]["skills"]) == expected_skills
    assert config["tools"]["allow"] == ["read", "write", "edit"]
    providers = (config.get("models") or {}).get("providers") or {}
    assert "baseUrl" not in providers.get("openai", {})
    assert auth_profiles["profiles"]["openai:default"]["keyRef"] == {
        "source": "env",
        "provider": "default",
        "id": "OPENAI_API_KEY",
    }
    assert runtime["notepatch_user_id"] == user_id
    assert "updated_at" not in runtime
    assert f"container_name: ${{OPENCLAW_CONTAINER_NAME:-notepatch-openclaw-{user_id}}}" in (
        root / "docker-compose.yml"
    ).read_text(encoding="utf-8")


def test_openclaw_runtime_provisioning_is_idempotent(client, db_sessionmaker):
    user_json = register_user(client, "openclaw-idempotent@example.com")
    user_id = user_json["user"]["id"]
    service = OpenClawUserRuntimeService()
    config_path = service.openclaw_json_path(user_id)
    auth_profiles_path = service.auth_profiles_path(user_id)
    env_path = service.env_path(user_id)
    token_before = [
        line.split("=", 1)[1]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("OPENCLAW_GATEWAY_TOKEN=")
    ][0]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["custom"] = {"keep": True}
    config["agents"]["defaults"]["model"] = "openai/old-model"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    with db_sessionmaker() as db:
        user = db.get(User, user_id)
        workspace = db.query(Workspace).filter_by(owner_user_id=user_id).one()
        service.provision_user(user, workspace)

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    token_after = [
        line.split("=", 1)[1]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("OPENCLAW_GATEWAY_TOKEN=")
    ][0]
    assert updated["custom"] == {"keep": True}
    assert updated["agents"]["defaults"]["model"] == {"primary": get_settings().openclaw_agent_model}
    assert token_after == token_before

    watched_mtimes = {
        path: path.stat().st_mtime_ns
        for path in (config_path, auth_profiles_path)
    }
    with db_sessionmaker() as db:
        user = db.get(User, user_id)
        workspace = db.query(Workspace).filter_by(owner_user_id=user_id).one()
        service.provision_user(user, workspace)
    assert {path: path.stat().st_mtime_ns for path in watched_mtimes} == watched_mtimes


def test_openclaw_runtime_provider_key_is_only_in_container_environment(client, db_sessionmaker):
    settings = get_settings()
    old_key = settings.openai_api_key
    settings.openai_api_key = "sk-not-a-real-test-key"
    try:
        user_json = register_user(client, "openclaw-provider-env@example.com")
        user_id = user_json["user"]["id"]
        service = OpenClawUserRuntimeService()
        root = Path(settings.openclaw_user_runtime_root) / "users" / user_id
        env_path = root / ".env"
        env_path.write_text(env_path.read_text(encoding="utf-8") + "OPENAI_API_KEY=old-secret\n", encoding="utf-8")

        with db_sessionmaker() as db:
            user = db.get(User, user_id)
            workspace = db.query(Workspace).filter_by(owner_user_id=user_id).one()
            service.provision_user(user, workspace)

        assert service.container_environment(user_id)["OPENAI_API_KEY"] == "sk-not-a-real-test-key"
        for path in (
            root / ".env",
            root / "docker-compose.yml",
            service.openclaw_json_path(user_id),
            service.auth_profiles_path(user_id),
        ):
            assert "sk-not-a-real-test-key" not in path.read_text(encoding="utf-8")
            assert "old-secret" not in path.read_text(encoding="utf-8")
    finally:
        settings.openai_api_key = old_key


def test_openclaw_runtime_writes_docker_socket_group_for_manual_compose(client, db_sessionmaker):
    settings = get_settings()
    old_socket_gid = settings.openclaw_docker_socket_gid
    settings.openclaw_docker_socket_gid = 125
    try:
        user_json = register_user(client, "openclaw-docker-group@example.com")
        user_id = user_json["user"]["id"]
        service = OpenClawUserRuntimeService()

        with db_sessionmaker() as db:
            user = db.get(User, user_id)
            workspace = db.query(Workspace).filter_by(owner_user_id=user_id).one()
            service.provision_user(user, workspace)

        root = Path(settings.openclaw_user_runtime_root) / "users" / user_id
        env_text = (root / ".env").read_text(encoding="utf-8")
        compose_text = (root / "docker-compose.yml").read_text(encoding="utf-8")
        assert "OPENCLAW_DOCKER_SOCKET_GID=125" in env_text
        assert "group_add:" in compose_text
        assert '- "${OPENCLAW_DOCKER_SOCKET_GID:-125}"' in compose_text
        assert service.container_group_add() == ["125"]
    finally:
        settings.openclaw_docker_socket_gid = old_socket_gid


def test_openclaw_runtime_writes_openai_base_url_from_env(client):
    settings = get_settings()
    old_base_url = settings.openai_base_url
    settings.openai_base_url = "https://proxy.example.com/v1"
    try:
        user = register_user(client, "openclaw-base-url@example.com")
        user_id = user["user"]["id"]
        config = json.loads(OpenClawUserRuntimeService().openclaw_json_path(user_id).read_text(encoding="utf-8"))
        assert config["models"]["providers"]["openai"]["baseUrl"] == "https://proxy.example.com/v1"
        assert config["models"]["providers"]["openai"]["models"] == []
        assert "OPENAI_API_KEY" not in json.dumps(config)
    finally:
        settings.openai_base_url = old_base_url


def test_openclaw_runtime_removes_openai_base_url_when_env_is_empty(client, db_sessionmaker):
    settings = get_settings()
    old_base_url = settings.openai_base_url
    settings.openai_base_url = "https://proxy.example.com/v1"
    try:
        user_json = register_user(client, "openclaw-base-url-empty@example.com")
        user_id = user_json["user"]["id"]
        service = OpenClawUserRuntimeService()
        assert json.loads(service.openclaw_json_path(user_id).read_text(encoding="utf-8"))["models"]["providers"][
            "openai"
        ]["baseUrl"] == "https://proxy.example.com/v1"
        assert json.loads(service.openclaw_json_path(user_id).read_text(encoding="utf-8"))["models"]["providers"][
            "openai"
        ]["models"] == []

        settings.openai_base_url = None
        with db_sessionmaker() as db:
            user = db.get(User, user_id)
            workspace = db.query(Workspace).filter_by(owner_user_id=user_id).one()
            service.provision_user(user, workspace)

        config = json.loads(service.openclaw_json_path(user_id).read_text(encoding="utf-8"))
        providers = (config.get("models") or {}).get("providers") or {}
        assert "baseUrl" not in providers.get("openai", {})
    finally:
        settings.openai_base_url = old_base_url


def _create_upload_session(client, token: str, workspace_id: str, filename: str) -> dict:
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": filename,
            "mime_type": "application/pdf",
            "file_size": 12,
            "document_kind": "homework",
            "title": filename,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_openclaw_document_sync_is_limited_to_current_user_workspace(
    client,
    db_sessionmaker,
    fake_storage,
):
    alice = register_user(client, "openclaw-sync-alice@example.com")
    bob = register_user(client, "openclaw-sync-bob@example.com")
    alice_workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])

    alice_upload = _create_upload_session(client, alice["access_token"], alice_workspace_id, "alice.pdf")
    bob_upload = _create_upload_session(client, bob["access_token"], bob_workspace_id, "bob.pdf")
    fake_storage.objects[(alice_upload["bucket"], alice_upload["object_key"])] = {
        "file_size": 12,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"alice document",
    }
    fake_storage.objects[(bob_upload["bucket"], bob_upload["object_key"])] = {
        "file_size": 10,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"bob doc",
    }

    artifact_id = str(uuid.uuid4())
    artifact_key = fake_storage.document_artifact_key(
        alice_workspace_id,
        alice_upload["document"]["id"],
        artifact_id,
        "ocr_json",
        "json",
    )
    fake_storage.objects[(alice_upload["bucket"], artifact_key)] = {
        "file_size": 2,
        "mime_type": "application/json",
        "metadata": {},
        "body": b"{}",
    }
    with db_sessionmaker() as db:
        db.get(Document, alice_upload["document"]["id"]).status = "uploaded"
        db.get(Document, bob_upload["document"]["id"]).status = "uploaded"
        db.add(
            DocumentArtifact(
                id=artifact_id,
                workspace_id=alice_workspace_id,
                document_id=alice_upload["document"]["id"],
                artifact_type="ocr_json",
                bucket=alice_upload["bucket"],
                object_key=artifact_key,
                mime_type="application/json",
                file_size=2,
                metadata_={"mock": True},
            )
        )
        db.commit()
        context = OpenClawUserRuntimeService().sync_workspace_documents(
            db=db,
            storage=fake_storage,
            workspace_id=alice_workspace_id,
            task_id="task-sync",
        )

    index_path = (
        Path(get_settings().openclaw_user_runtime_root)
        / "users"
        / alice["user"]["id"]
        / "workspace"
        / "notepatch"
        / "documents"
        / "index.json"
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    document_ids = {document["id"] for document in index["documents"]}
    assert document_ids == {alice_upload["document"]["id"]}
    assert bob_upload["document"]["id"] not in document_ids
    assert context["documents_synced"] == 1
    assert context["files_synced"] == 2
    assert context["documents_skipped"] == 0
    assert context["artifacts_skipped"] == 0
    assert index["skipped_documents"] == []
    assert index["skipped_artifacts"] == []
    assert (
        Path(get_settings().openclaw_user_runtime_root)
        / "users"
        / alice["user"]["id"]
        / "workspace"
        / "notepatch"
        / "documents"
        / alice_upload["document"]["id"]
        / "original"
        / "alice.pdf"
    ).exists()


def test_openclaw_document_sync_skips_created_and_bad_object_keys(client, db_sessionmaker, fake_storage):
    user = register_user(client, "openclaw-skip-created@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    created_upload = _create_upload_session(client, user["access_token"], workspace_id, "created.pdf")
    wrong_key_upload = _create_upload_session(client, user["access_token"], workspace_id, "wrong-key.pdf")

    with db_sessionmaker() as db:
        wrong_key_doc = db.get(Document, wrong_key_upload["document"]["id"])
        wrong_key_doc.status = "uploaded"
        wrong_key_doc.object_key = wrong_key_doc.object_key.replace(workspace_id, "old-workspace-id", 1)
        context = OpenClawUserRuntimeService().sync_workspace_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            task_id="task-skip-invalid",
        )

    reasons = {item["reason"] for item in context["skipped_documents"]}
    skipped_ids = {item["id"] for item in context["skipped_documents"]}
    assert skipped_ids == {created_upload["document"]["id"], wrong_key_upload["document"]["id"]}
    assert reasons == {"status_not_mirrorable", "object_key_outside_workspace"}
    assert context["documents_synced"] == 0
    assert context["files_synced"] == 0


def test_openclaw_document_sync_skips_uploaded_missing_object(client, db_sessionmaker, fake_storage):
    user = register_user(client, "openclaw-missing-object@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "missing.pdf")

    with db_sessionmaker() as db:
        db.get(Document, upload["document"]["id"]).status = "uploaded"
        context = OpenClawUserRuntimeService().sync_workspace_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            task_id="task-missing-object",
        )

    assert context["documents_synced"] == 0
    assert context["documents_skipped"] == 1
    assert context["skipped_documents"][0]["reason"] == "object_not_found"


def test_openclaw_document_sync_skips_missing_artifact(client, db_sessionmaker, fake_storage):
    user = register_user(client, "openclaw-missing-artifact@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "with-artifact.pdf")
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": 12,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"document",
    }
    artifact_id = str(uuid.uuid4())
    artifact_key = fake_storage.document_artifact_key(
        workspace_id,
        upload["document"]["id"],
        artifact_id,
        "ocr_json",
        "json",
    )

    with db_sessionmaker() as db:
        db.get(Document, upload["document"]["id"]).status = "uploaded"
        db.add(
            DocumentArtifact(
                id=artifact_id,
                workspace_id=workspace_id,
                document_id=upload["document"]["id"],
                artifact_type="ocr_json",
                bucket=upload["bucket"],
                object_key=artifact_key,
                mime_type="application/json",
                file_size=2,
                metadata_={},
            )
        )
        db.commit()
        context = OpenClawUserRuntimeService().sync_workspace_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            task_id="task-missing-artifact",
        )

    assert context["documents_synced"] == 1
    assert context["files_synced"] == 1
    assert context["artifacts_skipped"] == 1
    assert context["skipped_artifacts"][0]["reason"] == "object_not_found"


def test_openclaw_document_sync_exposes_ocr_text_paths(client, db_sessionmaker, fake_storage):
    user = register_user(client, "openclaw-ocr-text@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "ocr-ready.pdf")
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": 12,
        "mime_type": "application/pdf",
        "metadata": {},
        "body": b"document",
    }
    artifact_rows = []
    for artifact_type, ext, body, mime_type in (
        ("ocr_markdown", "md", b"# OCR", "text/markdown"),
        ("ocr_text", "txt", b"OCR", "text/plain"),
    ):
        artifact_id = str(uuid.uuid4())
        object_key = fake_storage.document_artifact_key(
            workspace_id,
            upload["document"]["id"],
            artifact_id,
            artifact_type,
            ext,
        )
        fake_storage.objects[(upload["bucket"], object_key)] = {
            "file_size": len(body),
            "mime_type": mime_type,
            "metadata": {},
            "body": body,
        }
        artifact_rows.append(
            DocumentArtifact(
                id=artifact_id,
                workspace_id=workspace_id,
                document_id=upload["document"]["id"],
                artifact_type=artifact_type,
                bucket=upload["bucket"],
                object_key=object_key,
                mime_type=mime_type,
                file_size=len(body),
                metadata_={"ocr_run_id": "run-1"},
            )
        )

    with db_sessionmaker() as db:
        db.get(Document, upload["document"]["id"]).status = "uploaded"
        db.add_all(artifact_rows)
        db.commit()
        OpenClawUserRuntimeService().sync_workspace_documents(
            db=db,
            storage=fake_storage,
            workspace_id=workspace_id,
            task_id="task-ocr-text",
        )

    index_path = (
        Path(get_settings().openclaw_user_runtime_root)
        / "users"
        / user["user"]["id"]
        / "workspace"
        / "notepatch"
        / "documents"
        / "index.json"
    )
    document = json.loads(index_path.read_text(encoding="utf-8"))["documents"][0]
    assert document["ocr_markdown_path"].endswith(f"{upload['document']['id']}/ocr/ocr.md")
    assert document["ocr_text_path"].endswith(f"{upload['document']['id']}/ocr/ocr.txt")
    assert (
        index_path.parent
        / upload["document"]["id"]
        / "ocr"
        / "ocr.md"
    ).read_bytes() == b"# OCR"


def test_openclaw_document_sync_fails_for_non_not_found_storage_errors(client, db_sessionmaker):
    user = register_user(client, "openclaw-storage-error@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_upload_session(client, user["access_token"], workspace_id, "storage-error.pdf")

    with db_sessionmaker() as db:
        db.get(Document, upload["document"]["id"]).status = "uploaded"
        with pytest.raises(OpenClawUserRuntimeError, match="storage service exploded"):
            OpenClawUserRuntimeService().sync_workspace_documents(
                db=db,
                storage=BrokenStorage(),
                workspace_id=workspace_id,
                task_id="task-storage-error",
            )
