from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.services.task_handlers import process_scan_document
from notepatch.modules.documents.services.scanner import (
    DocumentScanError,
    DocumentScanner,
    MalwareDetectedError,
    ScannerUnavailableError,
)
from notepatch.modules.learning.models.learning import LearningUnit, StudyNoteVersion
from notepatch.modules.learning.services.html_notes import validate_note_structure
from notepatch.modules.learning.services.note_render import NoteRenderService
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import Settings, get_settings
from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import PNG_BYTES


def test_public_path_prefix_validates_and_builds_note_urls():
    prefix = "/np-0123456789abcdef0123456789abcdef"
    settings = Settings(
        _env_file=None,
        public_path_prefix=prefix,
        public_api_base_url="",
        secret_key="test-secret-key-with-at-least-32-bytes",
    )
    assert settings.public_route_url("/api/v1/health") == f"{prefix}/api/v1/health"

    note = SimpleNamespace(
        id="note-id",
        workspace_id="workspace-id",
        learning_unit_id="unit-id",
        title="Public note",
    )
    renderer = NoteRenderService(settings)
    assert renderer.create_url(note).startswith(f"{prefix}/api/v1/assets/study-notes/render?")
    wrapped = renderer.wrap_html(note, "<article>Note</article>")
    assert f'href="{prefix}/api/v1/assets/note-themes/notepatch-paper-v1.css?v=' in wrapped

    absolute = Settings(
        _env_file=None,
        public_path_prefix=prefix,
        public_api_base_url=f"https://8.137.78.255{prefix}",
    )
    assert absolute.public_route_url("/api/v1/health") == (
        f"https://8.137.78.255{prefix}/api/v1/health"
    )
    with pytest.raises(ValueError):
        Settings(_env_file=None, public_path_prefix="/predictable")


def test_deployment_schema_revision_matches_alembic_head():
    repo_root = Path(__file__).resolve().parents[2]
    expected = Settings(_env_file=None).schema_revision
    alembic_config = Config(str(repo_root / "backend/alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(repo_root / "backend/migrations")
    )
    assert ScriptDirectory.from_config(alembic_config).get_heads() == [expected]
    assert f"SCHEMA_REVISION: {expected}" in (repo_root / "compose.yml").read_text()
    for dockerfile in (
        repo_root / "infra/docker/backend.Dockerfile",
        repo_root / "infra/docker/ocr-worker.Dockerfile",
    ):
        assert f"ARG SCHEMA_REVISION={expected}" in dockerfile.read_text()
    revisions = list((repo_root / "backend/migrations/versions").glob(f"{expected}_*.py"))
    assert len(revisions) == 1
    assert f"revision: str = \"{expected}\"" in revisions[0].read_text()


def test_public_gateway_preserves_external_tus_location():
    repo_root = Path(__file__).resolve().parents[2]
    compose = (repo_root / "compose.yml").read_text()
    nginx_template = (repo_root / "infra/proxy/notepatch-nginx-tls.conf.template").read_text()

    assert "- -behind-proxy" in compose
    assert "proxy_set_header X-Forwarded-Proto https;" in nginx_template
    assert "proxy_set_header X-Forwarded-Host $http_host;" in nginx_template
    assert (
        "proxy_redirect ~^https?://[^/]+/files/(.*)$ "
        "https://__PUBLIC_IP____PUBLIC_PATH_PREFIX__/files/$1;"
    ) in nginx_template


def test_versioned_note_theme_is_public_and_immutable(client):
    response = client.get("/api/v1/assets/note-themes/notepatch-paper-v1.css")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert ".np-note-theme" in response.text


@pytest.mark.parametrize(
    "theme_id", ["notepatch-paper-v1", "notepatch-paper-v2", "notepatch-paper-v3", "notepatch-paper-v4"]
)
def test_note_themes_define_all_supported_font_sizes(client, theme_id):
    response = client.get(f"/api/v1/assets/note-themes/{theme_id}.css")
    assert response.status_code == 200
    for size in (12, 14, 17, 20, 24, 28, 32, 40):
        assert f".np-font-size-{size}{{font-size:{size}px!important}}" in response.text


def test_signed_render_url_prefers_highlight_and_rejects_bad_token(client, db_sessionmaker, fake_storage):
    user = register_user(client, "note-render@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="Algebra")
        db.add(unit)
        db.flush()
        note = StudyNoteVersion(
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            version_no=1,
            title="Algebra Note",
            html_object_key="plain.html",
            json_object_key="note.json",
            highlighted_html_object_key="highlighted.html",
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        token_url = NoteRenderService().create_url(note, 900)
    fake_storage.objects[(fake_storage.bucket, "plain.html")] = {"body": b"<article class='np-note'>plain</article>"}
    fake_storage.objects[(fake_storage.bucket, "highlighted.html")] = {"body": b"<article class='np-note'>highlighted</article>"}
    response = client.get(token_url)
    assert response.status_code == 200
    assert "highlighted" in response.text
    assert "Content-Security-Policy" in response.headers
    assert client.get("/api/v1/assets/study-notes/render?token=bad-token-value").status_code == 401


def test_scholar_note_structure_requires_visual_contract():
    valid = (
        '<article class="np-note"><header><h1 class="np-note-title">Title</h1>'
        '<p class="np-note-summary">Summary</p></header>'
        '<section class="np-note-section" data-knowledge-point-id="point-1">Body</section></article>'
    )
    validate_note_structure(valid)
    with pytest.raises(ValueError):
        validate_note_structure("<p>markdown-like output</p>")


def test_document_scanner_hashes_detects_and_rejects_mime(tmp_path, monkeypatch):
    settings = get_settings()
    old_clamav = settings.clamav_enabled
    settings.clamav_enabled = True
    try:
        path = tmp_path / "image.png"
        path.write_bytes(PNG_BYTES)
        monkeypatch.setattr(DocumentScanner, "_clamav_scan", lambda self, value: None)
        result = DocumentScanner().scan(path, "image/png")
        assert len(result.sha256) == 64
        assert result.detected_mime_type == "image/png"
        with pytest.raises(DocumentScanError):
            DocumentScanner().scan(path, "application/pdf")
        monkeypatch.setattr(
            DocumentScanner,
            "_clamav_scan",
            lambda self, value: (_ for _ in ()).throw(MalwareDetectedError("Eicar-Signature")),
        )
        with pytest.raises(MalwareDetectedError):
            DocumentScanner().scan(path, "image/png")
        monkeypatch.setattr(
            DocumentScanner,
            "_clamav_scan",
            lambda self, value: (_ for _ in ()).throw(ScannerUnavailableError("clamav unavailable")),
        )
        with pytest.raises(ScannerUnavailableError):
            DocumentScanner().scan(path, "image/png")
    finally:
        settings.clamav_enabled = old_clamav


def test_disabled_scan_task_skips_storage_and_releases_chat_attachment(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    settings = get_settings()
    old_auto = settings.auto_learning_pipeline
    old_clamav = settings.clamav_enabled
    settings.auto_learning_pipeline = True
    settings.clamav_enabled = False
    try:
        user = register_user(client, "scan-disabled@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        owner_id = client.get("/api/v1/auth/me", headers=auth_headers(user["access_token"])).json()["id"]
        with db_sessionmaker() as db:
            document = Document(
                workspace_id=workspace_id,
                uploaded_by=owner_id,
                title="Chat image",
                original_filename="chat.png",
                mime_type="image/png",
                file_size=128,
                file_type="image",
                document_kind="chat_attachment",
                storage_backend="seaweedfs",
                bucket=fake_storage.bucket,
                object_key=f"workspaces/{workspace_id}/documents/test/original/chat.png",
                status="scanning",
                scan_status="pending",
            )
            db.add(document)
            db.flush()
            artifact = DocumentArtifact(
                workspace_id=workspace_id,
                document_id=document.id,
                artifact_type="original",
                bucket=document.bucket,
                object_key=document.object_key,
                mime_type=document.mime_type,
                file_size=document.file_size,
                metadata_={"source": "tusd"},
            )
            db.add(artifact)
            task, _ = TaskService(db).create_task_record(
                workspace_id=workspace_id,
                task_type="scan_document",
                resource_type="document",
                resource_id=document.id,
                payload={"document_id": document.id},
            )
            db.commit()
            task = TaskService(db).claim_task(task.id)
            assert task is not None

            def fail_download(*args, **kwargs):
                raise AssertionError("disabled scanning must not download the object")

            monkeypatch.setattr(fake_storage, "download_file", fail_download)
            process_scan_document(db, TaskService(db), task, fake_storage)

            db.refresh(document)
            db.refresh(artifact)
            db.refresh(task)
            assert document.status == "ready"
            assert document.scan_status == "skipped"
            assert document.sha256 is None
            assert document.detected_mime_type is None
            assert artifact.metadata_["scanner"] == "disabled"
            assert task.status == "succeeded"
            assert any(event.event_type == "scan_skipped" for event in task.events)

            infected = Document(
                workspace_id=workspace_id,
                uploaded_by=owner_id,
                title="Rejected image",
                original_filename="infected.png",
                mime_type="image/png",
                file_size=128,
                file_type="image",
                document_kind="chat_attachment",
                storage_backend="seaweedfs",
                bucket=fake_storage.bucket,
                object_key=f"workspaces/{workspace_id}/documents/infected/original/image.png",
                status="failed",
                scan_status="infected",
                scan_message="Malware detected",
            )
            db.add(infected)
            db.flush()
            infected_task, _ = TaskService(db).create_task_record(
                workspace_id=workspace_id,
                task_type="scan_document",
                resource_type="document",
                resource_id=infected.id,
                payload={"document_id": infected.id},
            )
            db.commit()
            infected_task = TaskService(db).claim_task(infected_task.id)
            assert infected_task is not None
            process_scan_document(db, TaskService(db), infected_task, fake_storage)
            db.refresh(infected)
            db.refresh(infected_task)
            assert infected.status == "failed"
            assert infected.scan_status == "infected"
            assert infected_task.status == "failed"
    finally:
        settings.auto_learning_pipeline = old_auto
        settings.clamav_enabled = old_clamav


def test_task_events_have_monotonic_sequence_numbers(db_sessionmaker):
    with db_sessionmaker() as db:
        task = TaskService(db).create_task_record(
            workspace_id=_workspace_id(db),
            task_type="build_knowledge_base",
        )[0]
        TaskService(db).add_event(task, "next", "next")
        db.commit()
        sequences = db.scalars(
            select(TaskEvent.sequence_no).where(TaskEvent.task_id == task.id).order_by(TaskEvent.sequence_no)
        ).all()
        assert sequences == [1, 2]


def _workspace_id(db):
    from notepatch.modules.identity.models.user import User
    from notepatch.modules.identity.models.workspace import Workspace
    from notepatch.platform.security import hash_password

    user = User(email="sequence@example.com", password_hash=hash_password("password123"))
    db.add(user)
    db.flush()
    workspace = Workspace(name="Personal", type="personal", owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    return workspace.id


def test_task_event_stream_resumes_and_closes_on_terminal(client, db_sessionmaker):
    user = register_user(client, "sse@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task, _queue = service.create_task_record(
            workspace_id=workspace_id,
            task_type="build_knowledge_base",
        )
        db.commit()
        task = service.claim_task(task.id)
        assert task is not None
        service.mark_succeeded(task, {"ok": True})
        task_id = task.id
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream",
        headers=auth_headers(user["access_token"]),
    )
    assert response.status_code == 200
    assert "event: task_event" in response.text
    assert "event: done" in response.text
    last_sequence = max(
        int(line.split(":", 1)[1].strip())
        for line in response.text.splitlines()
        if line.startswith("id:")
    )
    resumed = client.get(
        f"/api/v1/workspaces/{workspace_id}/tasks/{task_id}/events/stream",
        headers={**auth_headers(user["access_token"]), "Last-Event-ID": str(last_sequence)},
    )
    assert resumed.status_code == 200
    assert "event: task_event" not in resumed.text
    assert "event: done" in resumed.text


def test_unassigned_uploads_create_distinct_learning_units(client, db_sessionmaker, fake_storage):
    from notepatch.modules.tasks.services.executor import process_task
    from tests.test_doctr_worker import FailingDocTrClient
    from tests.test_learning_workflow import (
        _create_and_complete_document,
        _latest_task,
        _process_assignment_if_present,
    )

    settings = get_settings()
    old_auto = settings.auto_learning_pipeline
    settings.auto_learning_pipeline = True
    try:
        user = register_user(client, "separate-units@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        document_ids = []
        for filename in ("chapter-one.png", "chapter-two.png"):
            upload = _create_and_complete_document(
                client,
                fake_storage,
                user["access_token"],
                workspace_id,
                filename=filename,
                document_kind="courseware",
            )
            document_ids.append(upload["document"]["id"])
        with db_sessionmaker() as db:
            assert db.scalars(select(LearningUnit).where(LearningUnit.workspace_id == workspace_id)).all() == []
            for document_id in document_ids:
                process_task(
                    db,
                    _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id,
                    storage=fake_storage,
                    doctr_client=FailingDocTrClient(),
                )
                _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
            units = db.scalars(
                select(LearningUnit).where(LearningUnit.workspace_id == workspace_id)
            ).all()
            assert {unit.title for unit in units} == {"chapter-one.png", "chapter-two.png"}
    finally:
        settings.auto_learning_pipeline = old_auto


def test_learning_unit_merge_is_async_and_workspace_scoped(client, db_sessionmaker):
    alice = register_user(client, "merge-alice@example.com")
    bob = register_user(client, "merge-bob@example.com")
    alice_workspace = first_workspace_id(client, alice["access_token"])
    bob_workspace = first_workspace_id(client, bob["access_token"])
    with db_sessionmaker() as db:
        target = LearningUnit(workspace_id=alice_workspace, title="Target")
        source = LearningUnit(workspace_id=alice_workspace, title="Source")
        db.add_all([target, source])
        db.commit()
        target_id, source_id = target.id, source.id
    response = client.post(
        f"/api/v1/workspaces/{alice_workspace}/learning-units/{target_id}/merge",
        headers=auth_headers(alice["access_token"]),
        json={"source_learning_unit_ids": [source_id]},
    )
    assert response.status_code == 202
    assert response.json()["task_type"] == "merge_learning_units"
    assert response.json()["payload"]["source_learning_unit_ids"] == [source_id]
    cross_workspace = client.post(
        f"/api/v1/workspaces/{bob_workspace}/learning-units/{target_id}/merge",
        headers=auth_headers(bob["access_token"]),
        json={"source_learning_unit_ids": [source_id]},
    )
    assert cross_workspace.status_code == 404



def test_merge_retargets_homework_attempts_and_mistakes(db_sessionmaker, fake_storage):
    from notepatch.modules.identity.models.workspace import Workspace
    from notepatch.modules.learning.models.homework import Homework, Mistake
    from notepatch.modules.learning.models.learning import KnowledgePoint, KnowledgePointAttempt
    from notepatch.modules.learning.services.merge import LearningUnitMergeService

    with db_sessionmaker() as db:
        workspace_id = _workspace_id(db)
        owner_id = db.get(Workspace, workspace_id).owner_user_id
        target = LearningUnit(workspace_id=workspace_id, title="Target")
        source = LearningUnit(workspace_id=workspace_id, title="Source")
        db.add_all([target, source])
        db.flush()
        target_point = KnowledgePoint(
            workspace_id=workspace_id,
            learning_unit_id=target.id,
            name="Equations",
            normalized_name="equations",
        )
        db.add(target_point)
        db.flush()
        point = KnowledgePoint(
            workspace_id=workspace_id,
            learning_unit_id=source.id,
            name="Fractions",
            normalized_name="fractions",
        )
        db.add(point)
        db.flush()
        attempt = KnowledgePointAttempt(
            workspace_id=workspace_id,
            learning_unit_id=source.id,
            knowledge_point_id=point.id,
            outcome="incorrect",
            score_ratio=0.0,
        )
        mistake = Mistake(
            workspace_id=workspace_id,
            knowledge_point_id=point.id,
            description="Wrong fraction",
            metadata_={"learning_unit_id": source.id},
        )
        stray_attempt = KnowledgePointAttempt(
            workspace_id=workspace_id,
            learning_unit_id=source.id,
            knowledge_point_id=target_point.id,
            outcome="partial",
            score_ratio=0.5,
        )
        stray_mistake = Mistake(
            workspace_id=workspace_id,
            knowledge_point_id=target_point.id,
            description="Stale unit metadata",
            metadata_={"learning_unit_id": source.id},
        )
        homework = Homework(
            workspace_id=workspace_id,
            title="Fraction homework",
            created_by_user_id=owner_id,
            metadata_={"learning_unit_id": source.id},
        )
        db.add_all([attempt, mistake, stray_attempt, stray_mistake, homework])
        db.flush()

        service = LearningUnitMergeService(db, fake_storage)
        service._merge_knowledge_points(workspace_id, target.id, [source.id])
        service._retarget_homeworks(workspace_id, target.id, [target.id, source.id], [])
        db.flush()

        assert point.learning_unit_id == target.id
        assert attempt.learning_unit_id == target.id
        assert stray_attempt.learning_unit_id == target.id
        assert mistake.metadata_["learning_unit_id"] == target.id
        assert stray_mistake.metadata_["learning_unit_id"] == target.id
        assert homework.metadata_["learning_unit_id"] == target.id

def test_merge_status_closes_after_last_related_task(db_sessionmaker):
    from notepatch.modules.learning.models.learning import LearningUnitDocument
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = _workspace_id(db)
        unit = LearningUnit(workspace_id=workspace_id, title="Merged", merge_status="rebuilding")
        db.add(unit)
        db.flush()
        current, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="generate_flashcards",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        sibling, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        db.commit()
        current = TaskService(db).claim_task(current.id)
        assert current is not None
        TaskService(db).mark_succeeded(current, {"ok": True})
        reconcile_learning_unit_merge(db, current)
        db.refresh(unit)
        assert unit.merge_status == "rebuilding"

        sibling = TaskService(db).claim_task(sibling.id)
        assert sibling is not None
        TaskService(db).mark_succeeded(sibling, {"ok": True})
        reconcile_learning_unit_merge(db, sibling)
        db.refresh(unit)
        assert unit.merge_status == "completed"



def test_merge_status_recovers_when_recorded_failure_retry_succeeds(db_sessionmaker):
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = "workspace-merge-retry"
        failed_task_id = "failed-note-task"
        unit = LearningUnit(
            workspace_id=workspace_id,
            title="Merged",
            merge_status="failed",
            metadata_={"merge_failed_task_id": failed_task_id, "merge_failed_task_type": "generate_study_notes"},
        )
        db.add(unit)
        db.flush()
        retry = Task(
            workspace_id=workspace_id,
            task_type="generate_study_notes",
            status="succeeded",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id, "retry_of_task_id": failed_task_id},
        )
        db.add_all([unit, retry])
        db.commit()

        reconcile_learning_unit_merge(db, retry)
        db.refresh(unit)

        assert unit.merge_status == "completed"
        assert "merge_failed_task_id" not in unit.metadata_
        assert unit.metadata_["merge_completed_by_task_id"] == retry.id


def test_merge_status_does_not_recover_for_unrelated_success(db_sessionmaker):
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = "workspace-merge-unrelated"
        unit = LearningUnit(
            workspace_id=workspace_id,
            title="Merged",
            merge_status="failed",
            metadata_={"merge_failed_task_id": "expected-retry-target"},
        )
        db.add(unit)
        db.flush()
        unrelated = Task(
            workspace_id=workspace_id,
            task_type="generate_study_notes",
            status="succeeded",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        db.add_all([unit, unrelated])
        db.commit()

        reconcile_learning_unit_merge(db, unrelated)
        db.refresh(unit)

        assert unit.merge_status == "failed"

def test_merge_status_fails_with_related_terminal_failure(db_sessionmaker):
    from notepatch.modules.learning.services.merge import reconcile_learning_unit_merge

    with db_sessionmaker() as db:
        workspace_id = _workspace_id(db)
        unit = LearningUnit(workspace_id=workspace_id, title="Merged", merge_status="rebuilding")
        db.add(unit)
        db.flush()
        task, _ = TaskService(db).create_task_record(
            workspace_id=workspace_id,
            task_type="build_knowledge_base",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={"learning_unit_id": unit.id},
        )
        db.commit()
        task = TaskService(db).claim_task(task.id)
        assert task is not None
        TaskService(db).mark_failed(task, "upstream failed")
        reconcile_learning_unit_merge(db, task)
        db.refresh(unit)
        assert unit.merge_status == "failed"
        assert unit.metadata_["merge_failed_task_id"] == task.id
