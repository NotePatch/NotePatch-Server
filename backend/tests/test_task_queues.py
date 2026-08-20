from datetime import timedelta
from notepatch.platform.config import get_settings
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.services.queue import promote_due_retries, redis_key_for_queue, retry_key_for_queue
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task_lease import TaskLease, recover_orphaned_tasks, task_lease_key
from notepatch.platform.database import utcnow
from notepatch.entrypoints.worker import redis_keys_for_worker_queues, queue_names_from_args
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import PNG_BYTES


def _create_image_document(client, token: str, workspace_id: str, fake_storage):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/upload-session",
        headers=auth_headers(token),
        json={
            "filename": "ocr-worker.png",
            "mime_type": "image/png",
            "file_size": len(PNG_BYTES),
            "document_kind": "homework",
            "title": "OCR Worker",
        },
    )
    assert response.status_code == 201, response.text
    upload = response.json()
    fake_storage.objects[(upload["bucket"], upload["object_key"])] = {
        "file_size": len(PNG_BYTES),
        "mime_type": "image/png",
        "metadata": {},
        "body": PNG_BYTES,
    }
    return upload


def test_ocr_document_task_routes_to_ocr_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-ocr-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])

    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="ocr_document",
            resource_type="document",
            resource_id="document-id",
            payload={"document_id": "document-id"},
        )
        event = db.query(TaskEvent).filter_by(task_id=task.id, event_type="queued").one()

    assert fake_redis.lists == {"notepatch:tasks:ocr": [task.id]}
    assert event.data["queue"] == "ocr"
    assert event.data["redis_key"] == "notepatch:tasks:ocr"


def test_openclaw_backed_learning_tasks_route_to_ai_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-learning-ai-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])

    task_types = [
        "extract_questions",
        "build_knowledge_base",
        "generate_study_notes",
        "generate_flashcards",
        "grade_homework",
        "highlight_study_notes",
    ]
    with db_sessionmaker() as db:
        task_ids = [
            TaskService(db)
            .create_task(workspace_id=workspace_id, task_type=task_type, payload={})
            .id
            for task_type in task_types
        ]

    assert fake_redis.lists == {"notepatch:tasks:ai": task_ids}


def test_control_tasks_stay_on_default_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-default-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id, task_type="scan_document", payload={}
        )
    assert fake_redis.lists == {"notepatch:tasks": [task.id]}


def test_openclaw_chat_task_routes_to_chat_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-chat-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])

    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="openclaw_agent_run",
            payload={"prompt": "hello"},
        )
        event = db.query(TaskEvent).filter_by(task_id=task.id, event_type="queued").one()

    assert fake_redis.lists == {"notepatch:tasks:chat": [task.id]}
    assert event.data["queue"] == "chat"
    assert event.data["redis_key"] == "notepatch:tasks:chat"


def test_document_pipeline_routes_to_ocr_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-pipeline-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="document_processing_pipeline",
            payload={},
        )
    assert fake_redis.lists == {"notepatch:tasks:ocr": [task.id]}


def test_worker_queue_name_resolution_uses_configured_keys():
    settings = get_settings()
    old_worker_queues = settings.worker_queues
    settings.worker_queues = "default"
    try:
        assert queue_names_from_args(None) == ["default"]
        assert redis_keys_for_worker_queues(["default"]) == ["notepatch:tasks"]
        assert queue_names_from_args("ocr") == ["ocr"]
        assert redis_keys_for_worker_queues(["ocr"]) == ["notepatch:tasks:ocr"]
        assert queue_names_from_args("chat") == ["chat"]
        assert redis_keys_for_worker_queues(["chat"]) == ["notepatch:tasks:chat"]
        assert queue_names_from_args("ai") == ["ai"]
        assert redis_keys_for_worker_queues(["ai"]) == ["notepatch:tasks:ai"]
    finally:
        settings.worker_queues = old_worker_queues


def test_default_worker_queue_does_not_pop_ocr_tasks():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "ocr"), "ocr-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["default"]), timeout=0) is None
    assert fake_redis.lists[redis_key_for_queue(settings, "ocr")] == ["ocr-task-id"]


def test_default_worker_queue_does_not_pop_chat_tasks():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "chat"), "chat-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["default"]), timeout=0) is None
    assert fake_redis.lists[redis_key_for_queue(settings, "chat")] == ["chat-task-id"]


def test_chat_worker_queue_can_pop_openclaw_task():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "chat"), "chat-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["chat"]), timeout=0) == (
        "notepatch:tasks:chat",
        "chat-task-id",
    )


def test_chat_worker_does_not_pop_learning_ai_tasks():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "ai"), "learning-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["chat"]), timeout=0) is None
    assert fake_redis.lists[redis_key_for_queue(settings, "ai")] == ["learning-task-id"]


def test_ai_worker_queue_can_pop_learning_task():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "ai"), "learning-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["ai"]), timeout=0) == (
        "notepatch:tasks:ai",
        "learning-task-id",
    )


def test_ocr_worker_queue_can_pop_and_process_ocr_document(client, db_sessionmaker, fake_storage):
    settings = get_settings()
    fake_redis = FakeRedis()
    user = register_user(client, "queue-ocr-worker@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_image_document(client, user["access_token"], workspace_id, fake_storage)

    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="ocr_document",
            resource_type="document",
            resource_id=upload["document"]["id"],
            payload={"document_id": upload["document"]["id"]},
            enqueue=False,
        )
        fake_redis.rpush(redis_key_for_queue(settings, "ocr"), task.id)
        popped = fake_redis.brpop(redis_keys_for_worker_queues(["ocr"]), timeout=0)
        assert popped == ("notepatch:tasks:ocr", task.id)
        processed = process_task(db, task.id, storage=fake_storage)
        assert processed.status == "succeeded"
        db.expire_all()
        artifacts = db.query(Task).filter_by(id=task.id).one().result["ocr_artifacts"]

    assert set(artifacts) == {
        "ocr_json",
        "ocr_markdown",
        "ocr_text",
        "layout_json",
        "formula_json",
        "tables_json",
    }


def test_ocr_document_stays_queued_when_only_default_worker_is_available(client, db_sessionmaker, fake_storage):
    settings = get_settings()
    fake_redis = FakeRedis()
    user = register_user(client, "queue-no-ocr-worker@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_image_document(client, user["access_token"], workspace_id, fake_storage)

    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="ocr_document",
            resource_type="document",
            resource_id=upload["document"]["id"],
            payload={"document_id": upload["document"]["id"]},
            enqueue=False,
        )
        fake_redis.rpush(redis_key_for_queue(settings, "ocr"), task.id)
        assert fake_redis.brpop(redis_keys_for_worker_queues(["default"]), timeout=0) is None
        db.refresh(task)
        assert task.status == "queued"


def test_retry_is_delayed_then_promoted_to_original_queue(client, db_sessionmaker, monkeypatch):
    settings = get_settings()
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-retry@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task = service.create_task(
            workspace_id=workspace_id,
            task_type="ocr_document",
            payload={"document_id": "missing"},
            enqueue=False,
        )
        task = service.claim_task(task.id)
        assert task is not None
        assert service.schedule_retry(task, "model temporarily unavailable") is True
        assert task.status == "queued"
        assert task.attempt == 1
        retry_key = retry_key_for_queue(settings, "ocr")
        assert task.id in fake_redis.zsets[retry_key]
        promoted = promote_due_retries(
            fake_redis,
            settings,
            ["ocr"],
            now=task.next_attempt_at.timestamp() + 1,
        )
        assert promoted == 1
        assert fake_redis.lists[redis_key_for_queue(settings, "ocr")] == [task.id]


def test_success_after_retry_clears_previous_error(client, db_sessionmaker):
    user = register_user(client, "queue-retry-success@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    with db_sessionmaker() as db:
        service = TaskService(db)
        task = service.create_task(
            workspace_id=workspace_id,
            task_type="build_knowledge_base",
            enqueue=False,
        )
        task = service.claim_task(task.id)
        assert task is not None
        task.error_message = "temporary gateway failure"
        db.commit()

        service.mark_succeeded(task, {"ok": True})

        db.refresh(task)
        assert task.status == "succeeded"
        assert task.error_message is None


def test_worker_task_lease_prevents_duplicate_execution():
    fake_redis = FakeRedis()
    with TaskLease(fake_redis, "leased-task") as first:
        assert first.acquired is True
        with TaskLease(fake_redis, "leased-task") as duplicate:
            assert duplicate.acquired is False
    assert task_lease_key("leased-task") not in fake_redis.values


def test_orphaned_running_task_is_requeued(client, db_sessionmaker):
    settings = get_settings()
    old_grace = settings.task_orphan_recovery_grace_seconds
    settings.task_orphan_recovery_grace_seconds = 30
    fake_redis = FakeRedis()
    user = register_user(client, "orphan-requeue@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    try:
        with db_sessionmaker() as db:
            task, _ = TaskService(db).create_task_record(
                workspace_id=workspace_id,
                task_type="generate_flashcards",
                payload={},
            )
            task.status = "running"
            task.attempt = 1
            task.started_at = utcnow() - timedelta(seconds=60)
            db.commit()

            assert recover_orphaned_tasks(fake_redis, db, ["ai"]) == 1
            db.refresh(task)
            assert task.status == "queued"
            assert task.attempt == 1
            assert fake_redis.lists["notepatch:tasks:ai"] == [task.id]
            event = db.query(TaskEvent).filter_by(task_id=task.id, event_type="orphan_requeued").one()
            assert event.data["attempt"] == 1
    finally:
        settings.task_orphan_recovery_grace_seconds = old_grace


def test_running_task_with_live_lease_is_not_requeued(client, db_sessionmaker):
    settings = get_settings()
    old_grace = settings.task_orphan_recovery_grace_seconds
    settings.task_orphan_recovery_grace_seconds = 1
    fake_redis = FakeRedis()
    user = register_user(client, "orphan-live-lease@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    try:
        with db_sessionmaker() as db:
            task, _ = TaskService(db).create_task_record(
                workspace_id=workspace_id,
                task_type="generate_study_notes",
                payload={},
            )
            task.status = "running"
            task.attempt = 1
            task.started_at = utcnow() - timedelta(seconds=60)
            db.commit()
            fake_redis.set(task_lease_key(task.id), "worker", ex=60)

            assert recover_orphaned_tasks(fake_redis, db, ["ai"]) == 0
            db.refresh(task)
            assert task.status == "running"
    finally:
        settings.task_orphan_recovery_grace_seconds = old_grace


def test_orphaned_task_at_attempt_limit_fails(client, db_sessionmaker):
    settings = get_settings()
    old_grace = settings.task_orphan_recovery_grace_seconds
    settings.task_orphan_recovery_grace_seconds = 1
    fake_redis = FakeRedis()
    user = register_user(client, "orphan-exhausted@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    try:
        with db_sessionmaker() as db:
            task, _ = TaskService(db).create_task_record(
                workspace_id=workspace_id,
                task_type="generate_flashcards",
                payload={},
                max_attempts=1,
            )
            task.status = "running"
            task.attempt = 1
            task.started_at = utcnow() - timedelta(seconds=60)
            db.commit()

            assert recover_orphaned_tasks(fake_redis, db, ["ai"]) == 1
            db.refresh(task)
            assert task.status == "failed"
            assert "attempt limit" in task.error_message
    finally:
        settings.task_orphan_recovery_grace_seconds = old_grace
