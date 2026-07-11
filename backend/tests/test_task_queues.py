from notepatch.platform.config import get_settings
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.services.queue import promote_due_retries, redis_key_for_queue, retry_key_for_queue
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.tasks.services.executor import process_task
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


def test_non_ocr_tasks_route_to_default_queue(client, db_sessionmaker, monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *args, **kwargs: fake_redis)
    user = register_user(client, "queue-default-route@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])

    task_types = [
        "grade_homework",
        "openclaw_agent_run",
        "build_knowledge_base",
        "generate_flashcards",
    ]
    with db_sessionmaker() as db:
        task_ids = [
            TaskService(db)
            .create_task(workspace_id=workspace_id, task_type=task_type, payload={})
            .id
            for task_type in task_types
        ]

    assert fake_redis.lists == {"notepatch:tasks": task_ids}


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


def test_worker_queue_name_resolution_uses_default_or_ocr_keys():
    settings = get_settings()
    old_worker_queues = settings.worker_queues
    settings.worker_queues = "default"
    try:
        assert queue_names_from_args(None) == ["default"]
        assert redis_keys_for_worker_queues(["default"]) == ["notepatch:tasks"]
        assert queue_names_from_args("ocr") == ["ocr"]
        assert redis_keys_for_worker_queues(["ocr"]) == ["notepatch:tasks:ocr"]
    finally:
        settings.worker_queues = old_worker_queues


def test_default_worker_queue_does_not_pop_ocr_tasks():
    settings = get_settings()
    fake_redis = FakeRedis()
    fake_redis.rpush(redis_key_for_queue(settings, "ocr"), "ocr-task-id")

    assert fake_redis.brpop(redis_keys_for_worker_queues(["default"]), timeout=0) is None
    assert fake_redis.lists[redis_key_for_queue(settings, "ocr")] == ["ocr-task-id"]


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
