from collections.abc import Generator
import fnmatch
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from notepatch.entrypoints.deps import get_storage_service, get_task_service
from notepatch.platform.config import get_settings
from notepatch.platform.database import Base, get_db
from notepatch.entrypoints.api import app
from notepatch.platform.storage import StorageService
from notepatch.modules.tasks.services.task import TaskService


class FakeStorage:
    def __init__(self) -> None:
        self.bucket = "notepatch-test"
        self.objects: dict[tuple[str, str], dict] = {}

    document_original_key = staticmethod(StorageService.document_original_key)
    document_artifact_key = staticmethod(StorageService.document_artifact_key)
    document_processed_key = staticmethod(StorageService.document_processed_key)
    sandbox_input_key = staticmethod(StorageService.sandbox_input_key)
    sandbox_output_key = staticmethod(StorageService.sandbox_output_key)
    user_avatar_key = staticmethod(StorageService.user_avatar_key)
    learning_unit_note_key = staticmethod(StorageService.learning_unit_note_key)
    filename_for_object_key = staticmethod(StorageService.filename_for_object_key)

    def create_presigned_download_url(self, bucket: str, object_key: str, expires_seconds: int | None = None) -> str:
        return f"mock://download/{bucket}/{object_key}?expires={expires_seconds}"

    def create_presigned_artifact_download_url(
        self,
        bucket: str,
        object_key: str,
        expires_seconds: int | None = None,
    ) -> str:
        return self.create_presigned_download_url(bucket, object_key, expires_seconds)

    def object_exists(self, bucket: str, object_key: str) -> bool:
        return (bucket, object_key) in self.objects

    def get_object_metadata(self, bucket: str, object_key: str) -> dict:
        item = self.objects[(bucket, object_key)]
        return {
            "content_length": item.get("file_size"),
            "content_type": item.get("mime_type"),
            "metadata": item.get("metadata", {}),
        }

    def bucket_exists(self, bucket: str | None = None) -> bool:
        return True

    def delete_object(self, bucket: str, object_key: str) -> None:
        self.objects.pop((bucket, object_key), None)

    def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> None:
        self.objects[(dst_bucket, dst_key)] = dict(self.objects[(src_bucket, src_key)])

    def put_file(
        self,
        bucket: str,
        object_key: str,
        file_path: str | Path,
        *,
        content_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        path = Path(file_path)
        self.objects[(bucket, object_key)] = {
            "file_size": path.stat().st_size,
            "mime_type": content_type,
            "metadata": metadata or {},
            "body": path.read_bytes(),
        }

    def download_file(self, bucket: str, object_key: str, dest_path: str | Path) -> None:
        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[(bucket, object_key)]["body"])

    def put_json_artifact(self, object_key: str, payload: dict, bucket: str | None = None) -> None:
        self.objects[(bucket or self.bucket, object_key)] = {
            "file_size": len(str(payload).encode("utf-8")),
            "mime_type": "application/json",
            "metadata": {},
            "body": payload,
        }

    def get_json_artifact(self, object_key: str, bucket: str | None = None) -> dict:
        payload = self.objects[(bucket or self.bucket, object_key)]["body"]
        if not isinstance(payload, dict):
            raise ValueError("JSON artifact must contain an object")
        return dict(payload)

    def get_text_artifact(self, object_key: str, bucket: str | None = None) -> str:
        payload = self.objects[(bucket or self.bucket, object_key)]["body"]
        return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)

    def delete_prefix(self, prefix: str) -> None:
        for key in list(self.objects):
            if key[1].startswith(prefix):
                del self.objects[key]


class FakeRedis:
    def __init__(self, *, fail_reads: bool = False) -> None:
        self.fail_reads = fail_reads
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.lists: dict[str, list[str]] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value

    def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key: str):
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    def delete(self, key: str) -> int:
        existed = key in self.values
        self.values.pop(key, None)
        return int(existed)

    def scan_iter(self, match: str):
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zsets.setdefault(key, {}).update(mapping)

    def zscore(self, key: str, member: str) -> float | None:
        return self.zsets.get(key, {}).get(member)

    def zrange(self, key: str, start: int, end: int):
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        items = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        values = [item[0] for item in items]
        if end == -1:
            return values[start:]
        return values[start : end + 1]

    def zrangebyscore(self, key: str, minimum, maximum):
        upper = float(maximum)
        return [
            member
            for member, score in sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
            if score <= upper
        ]

    def zrem(self, key: str, member: str) -> int:
        existed = member in self.zsets.get(key, {})
        self.zsets.get(key, {}).pop(member, None)
        return int(existed)

    def eval(self, script: str, number_of_keys: int, key: str, token: str, *args):
        if self.values.get(key) != token:
            return 0
        if "del" in script:
            self.values.pop(key, None)
        return 1

    def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def brpop(self, keys, timeout: int = 0):
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        if isinstance(keys, (str, bytes)):
            keys = [keys]
        for key in keys:
            key_text = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            values = self.lists.get(key_text) or []
            if values:
                return key_text, values.pop()
        return None

    def llen(self, key: str) -> int:
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        return len(self.lists.get(key, []))

    def lrem(self, key: str, count: int, value: str) -> int:
        values = self.lists.get(key, [])
        removed = values.count(value) if count == 0 else min(values.count(value), abs(count))
        remaining = removed
        if count < 0:
            values.reverse()
        kept = []
        for item in values:
            if item == value and remaining:
                remaining -= 1
            else:
                kept.append(item)
        if count < 0:
            kept.reverse()
        self.lists[key] = kept
        return removed

    def ping(self) -> bool:
        if self.fail_reads:
            raise RuntimeError("redis unavailable")
        return True


class NoQueueTaskService(TaskService):
    def enqueue_task(self, task_id: str, queue_name: str | None = None) -> bool:
        return True


@pytest.fixture
def db_sessionmaker() -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        yield TestingSessionLocal
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(
    db_sessionmaker: sessionmaker[Session],
    fake_storage: FakeStorage,
    tmp_path: Path,
    monkeypatch,
) -> Generator[TestClient, None, None]:
    settings = get_settings()
    old_tusd_data_dir = settings.tusd_data_dir
    old_openclaw_user_runtime_root = settings.openclaw_user_runtime_root
    old_openai_api_key = settings.openai_api_key
    old_openai_base_url = settings.openai_base_url
    old_ocr_engine = settings.ocr_engine
    old_ocr_temp_dir = settings.ocr_temp_dir
    old_gpu_lock_enabled = settings.gpu_lock_enabled
    old_admin_emails = settings.admin_emails
    old_auto_learning_pipeline = settings.auto_learning_pipeline
    old_clamav_enabled = settings.clamav_enabled
    old_public_api_base_url = settings.public_api_base_url
    old_public_path_prefix = settings.public_path_prefix
    settings.tusd_data_dir = str(tmp_path)
    settings.openclaw_user_runtime_root = str(tmp_path / "openclaw-users")
    settings.openai_api_key = None
    settings.openai_base_url = None
    settings.ocr_engine = "paddleocr"
    settings.ocr_temp_dir = str(tmp_path / "ocr")
    settings.gpu_lock_enabled = False
    settings.admin_emails = ""
    settings.auto_learning_pipeline = False
    settings.clamav_enabled = False
    settings.public_api_base_url = ""
    settings.public_path_prefix = ""
    from tests.fakes import FakeEmbeddingClient, FakeSkillRunner, fake_ocr_pipeline

    monkeypatch.setattr("notepatch.modules.tasks.services.executor.OcrPipeline", fake_ocr_pipeline)
    monkeypatch.setattr("notepatch.modules.tasks.services.executor.EmbeddingClient", FakeEmbeddingClient)
    monkeypatch.setattr("notepatch.modules.tasks.services.executor.OpenClawSkillRunner", FakeSkillRunner)
    task_queue_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *args, **kwargs: task_queue_redis,
    )

    def override_get_db() -> Generator[Session, None, None]:
        db = db_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    def override_get_task_service(db: Session = Depends(get_db)) -> NoQueueTaskService:
        return NoQueueTaskService(db)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_storage_service] = lambda: fake_storage
    app.dependency_overrides[get_task_service] = override_get_task_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    settings.tusd_data_dir = old_tusd_data_dir
    settings.openclaw_user_runtime_root = old_openclaw_user_runtime_root
    settings.openai_api_key = old_openai_api_key
    settings.openai_base_url = old_openai_base_url
    settings.ocr_engine = old_ocr_engine
    settings.ocr_temp_dir = old_ocr_temp_dir
    settings.gpu_lock_enabled = old_gpu_lock_enabled
    settings.admin_emails = old_admin_emails
    settings.auto_learning_pipeline = old_auto_learning_pipeline
    settings.clamav_enabled = old_clamav_enabled
    settings.public_api_base_url = old_public_api_base_url
    settings.public_path_prefix = old_public_path_prefix


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def register_user(client: TestClient, email: str, password: str = "password123") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def first_workspace_id(client: TestClient, access_token: str) -> str:
    response = client.get("/api/v1/workspaces", headers=auth_headers(access_token))
    assert response.status_code == 200, response.text
    workspaces = response.json()
    assert len(workspaces) == 1
    return workspaces[0]["id"]
