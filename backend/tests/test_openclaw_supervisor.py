import json
import time

from sqlalchemy import select

from notepatch.platform.config import get_settings
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.ai.services.supervisor import OpenClawContainerManager, OpenClawSupervisor
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.identity.services.presence import PresenceService
from notepatch.modules.admin.models.admin import AdminOperation
from tests.conftest import FakeRedis, register_user


class NotFound(Exception):
    pass


class FakeContainer:
    def __init__(self, store, name: str, kwargs: dict) -> None:
        self.store = store
        self.name = name
        self.kwargs = kwargs
        self.labels = kwargs["labels"]
        self.status = "running"
        self.started = 0
        self.stopped = 0
        self.removed = False
        self.attrs = kwargs.get("attrs", {})

    def reload(self) -> None:
        return None

    def start(self) -> None:
        self.status = "running"
        self.started += 1

    def stop(self, timeout: int = 10) -> None:
        self.status = "exited"
        self.stopped += 1

    def remove(self, force: bool = False) -> None:
        self.removed = True
        self.store.pop(self.name, None)


class FakeContainers:
    def __init__(self) -> None:
        self.items: dict[str, FakeContainer] = {}
        self.run_calls: list[dict] = []

    def get(self, name: str) -> FakeContainer:
        if name not in self.items:
            raise NotFound(name)
        return self.items[name]

    def run(self, image: str, **kwargs) -> FakeContainer:
        container = FakeContainer(self.items, kwargs["name"], {"image": image, **kwargs})
        self.items[container.name] = container
        self.run_calls.append({"image": image, **kwargs})
        return container

    def list(self, all: bool = False, filters: dict | None = None) -> list[FakeContainer]:
        containers = list(self.items.values())
        labels = (filters or {}).get("label") or []
        for label in labels:
            key, value = label.split("=", 1)
            containers = [container for container in containers if container.labels.get(key) == value]
        return containers


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers()


def _registered_user_and_workspace(client, db_sessionmaker, email: str) -> tuple[str, str]:
    user_json = register_user(client, email)
    with db_sessionmaker() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.owner_user_id == user_json["user"]["id"]))
        assert workspace is not None
        return user_json["user"]["id"], workspace.id


def _run_supervisor_once(db_sessionmaker, presence: PresenceService, docker_client: FakeDockerClient) -> None:
    manager = OpenClawContainerManager(docker_client=docker_client)
    supervisor = OpenClawSupervisor(
        db_factory=db_sessionmaker,
        presence=presence,
        container_manager=manager,
    )
    supervisor.run_once()


def test_supervisor_starts_gateway_for_online_user(client, db_sessionmaker):
    user_id, workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-online@example.com")
    settings = get_settings()
    old_key = settings.openai_api_key
    old_socket_gid = settings.openclaw_docker_socket_gid
    settings.openai_api_key = "sk-supervisor-test-key"
    settings.openclaw_docker_socket_gid = 125
    docker_client = FakeDockerClient()
    presence = PresenceService(redis_client=FakeRedis())
    presence.heartbeat(user_id, "tab-1")

    try:
        _run_supervisor_once(db_sessionmaker, presence, docker_client)
    finally:
        settings.openai_api_key = old_key
        settings.openclaw_docker_socket_gid = old_socket_gid

    container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")
    run_kwargs = docker_client.containers.run_calls[0]
    assert run_kwargs["mem_limit"] == get_settings().openclaw_gateway_memory_limit
    assert run_kwargs["nano_cpus"] == get_settings().openclaw_gateway_nano_cpus
    assert run_kwargs["pids_limit"] == get_settings().openclaw_gateway_pids_limit
    assert run_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert run_kwargs["cap_drop"] == ["ALL"]
    assert container.status == "running"
    assert run_kwargs["network"] == "notepatch-server_default"
    assert run_kwargs["labels"]["notepatch.user_id"] == user_id
    assert run_kwargs["labels"]["notepatch.workspace_id"] == workspace_id
    assert run_kwargs["labels"]["notepatch.config_hash"]
    assert "ports" not in run_kwargs
    assert "/var/run/docker.sock" in run_kwargs["volumes"]
    assert run_kwargs["group_add"] == ["125"]
    assert run_kwargs["environment"]["OPENCLAW_GATEWAY_TOKEN"].startswith("notepatch-")
    assert run_kwargs["environment"]["OPENAI_API_KEY"] == "sk-supervisor-test-key"


def test_supervisor_is_idempotent_for_running_matching_container(client, db_sessionmaker):
    user_id, _workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-idempotent@example.com")
    docker_client = FakeDockerClient()
    presence = PresenceService(redis_client=FakeRedis())
    presence.heartbeat(user_id, "tab-1")

    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    service = OpenClawUserRuntimeService()
    watched_mtimes = {
        path: path.stat().st_mtime_ns
        for path in (service.openclaw_json_path(user_id), service.auth_profiles_path(user_id))
    }
    _run_supervisor_once(db_sessionmaker, presence, docker_client)

    assert len(docker_client.containers.run_calls) == 1
    assert {path: path.stat().st_mtime_ns for path in watched_mtimes} == watched_mtimes


def test_supervisor_does_not_recreate_gateway_for_hot_runtime_config_change(client, db_sessionmaker):
    user_id, _workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-recreate@example.com")
    docker_client = FakeDockerClient()
    presence = PresenceService(redis_client=FakeRedis())
    presence.heartbeat(user_id, "tab-1")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    first_container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")

    service = OpenClawUserRuntimeService()
    config_path = service.openclaw_json_path(user_id)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["custom"] = {"changed": True}
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)

    assert first_container.removed is False
    assert len(docker_client.containers.run_calls) == 1


def test_supervisor_ignores_gateway_managed_config_fields(client, db_sessionmaker):
    user_id, _workspace_id = _registered_user_and_workspace(
        client,
        db_sessionmaker,
        "supervisor-gateway-managed@example.com",
    )
    docker_client = FakeDockerClient()
    presence = PresenceService(redis_client=FakeRedis())
    presence.heartbeat(user_id, "tab-1")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    first_container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")

    service = OpenClawUserRuntimeService()
    config_path = service.openclaw_json_path(user_id)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["plugins"] = {"entries": {"openai": {"enabled": True}}}
    config["meta"] = {"lastTouchedVersion": "2026.4.5", "lastTouchedAt": "volatile"}
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)

    assert first_container.removed is False
    assert len(docker_client.containers.run_calls) == 1


def test_supervisor_recreates_gateway_when_openai_base_url_changes(client, db_sessionmaker):
    settings = get_settings()
    old_base_url = settings.openai_base_url
    settings.openai_base_url = None
    try:
        user_id, _workspace_id = _registered_user_and_workspace(
            client, db_sessionmaker, "supervisor-base-url@example.com"
        )
        docker_client = FakeDockerClient()
        presence = PresenceService(redis_client=FakeRedis())
        presence.heartbeat(user_id, "tab-1")
        _run_supervisor_once(db_sessionmaker, presence, docker_client)
        first_container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")

        settings.openai_base_url = "https://proxy.example.com/v1"
        _run_supervisor_once(db_sessionmaker, presence, docker_client)

        assert first_container.removed is True
        assert len(docker_client.containers.run_calls) == 2
        assert docker_client.containers.run_calls[1]["environment"]["OPENAI_BASE_URL"] == "https://proxy.example.com/v1"
        config_path = OpenClawUserRuntimeService().openclaw_json_path(user_id)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["models"]["providers"]["openai"]["baseUrl"] == "https://proxy.example.com/v1"
        assert config["models"]["providers"]["openai"]["models"][0]["input"] == ["text", "image"]
    finally:
        settings.openai_base_url = old_base_url


def test_supervisor_removes_only_the_users_stale_sandbox_on_gateway_rebuild(
    client, db_sessionmaker
):
    settings = get_settings()
    old_base_url = settings.openai_base_url
    settings.openai_base_url = None
    try:
        user_id, _workspace_id = _registered_user_and_workspace(
            client, db_sessionmaker, "supervisor-sandbox-cleanup@example.com"
        )
        other_user_id, _ = _registered_user_and_workspace(
            client, db_sessionmaker, "supervisor-sandbox-other@example.com"
        )
        docker_client = FakeDockerClient()
        presence = PresenceService(redis_client=FakeRedis())
        presence.heartbeat(user_id, "tab-1")
        _run_supervisor_once(db_sessionmaker, presence, docker_client)

        runtime = OpenClawUserRuntimeService()
        owned = FakeContainer(
            docker_client.containers.items,
            "openclaw-sbx-agent-main-owned",
            {
                "labels": {"openclaw.sandbox": "1"},
                "attrs": {
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(runtime.workspace_dir(user_id).resolve()),
                            "Destination": "/workspace",
                        }
                    ]
                },
            },
        )
        other = FakeContainer(
            docker_client.containers.items,
            "openclaw-sbx-agent-main-other",
            {
                "labels": {"openclaw.sandbox": "1"},
                "attrs": {
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(runtime.workspace_dir(other_user_id).resolve()),
                            "Destination": "/workspace",
                        }
                    ]
                },
            },
        )
        docker_client.containers.items[owned.name] = owned
        docker_client.containers.items[other.name] = other

        settings.openai_base_url = "https://changed.example.com/v1"
        _run_supervisor_once(db_sessionmaker, presence, docker_client)

        assert owned.removed is True
        assert other.removed is False
    finally:
        settings.openai_base_url = old_base_url


def test_supervisor_removes_runtime_for_user_purge(client, db_sessionmaker):
    user_id, workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-purge@example.com")
    docker_client = FakeDockerClient()
    manager = OpenClawContainerManager(docker_client=docker_client)
    with db_sessionmaker() as db:
        user = db.get(User, user_id)
        workspace = db.get(Workspace, workspace_id)
        manager.ensure_running(user, workspace)
        operation = AdminOperation(
            actor_user_id=user_id,
            actor_workspace_id=workspace_id,
            operation_type="purge_user",
            target_type="user",
            target_id=user_id,
            status="running",
            phase="runtime_cleanup_requested",
            payload={},
        )
        db.add(operation)
        db.commit()
        operation_id = operation.id

    OpenClawSupervisor(
        db_factory=db_sessionmaker,
        presence=PresenceService(redis_client=FakeRedis()),
        container_manager=manager,
    ).run_once()

    with db_sessionmaker() as db:
        assert db.get(AdminOperation, operation_id).phase == "runtime_cleanup_completed"
    assert manager._get_container(f"notepatch-openclaw-{user_id}") is None
    assert not manager.runtime.user_root(user_id).exists()


def test_supervisor_stops_gateway_after_offline_grace(client, db_sessionmaker):
    user_id, _workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-offline@example.com")
    docker_client = FakeDockerClient()
    redis = FakeRedis()
    presence = PresenceService(redis_client=redis)
    presence.heartbeat(user_id, "tab-1")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")

    presence.offline(user_id, "tab-1")
    redis.zadd(PresenceService.last_seen_key, {user_id: time.time() - 1000})
    _run_supervisor_once(db_sessionmaker, presence, docker_client)

    assert container.status == "exited"
    assert container.stopped == 1


def test_supervisor_keeps_offline_gateway_running_for_active_openclaw_task(client, db_sessionmaker):
    user_id, workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-task@example.com")
    docker_client = FakeDockerClient()
    redis = FakeRedis()
    presence = PresenceService(redis_client=redis)
    presence.heartbeat(user_id, "tab-1")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")
    presence.offline(user_id, "tab-1")
    redis.zadd(PresenceService.last_seen_key, {user_id: time.time() - 1000})
    with db_sessionmaker() as db:
        db.add(Task(workspace_id=workspace_id, task_type="openclaw_agent_run", status="queued", payload={}))
        db.commit()

    _run_supervisor_once(db_sessionmaker, presence, docker_client)

    assert container.status == "running"
    assert container.stopped == 0


def test_supervisor_does_not_stop_containers_when_redis_reads_fail(client, db_sessionmaker):
    user_id, _workspace_id = _registered_user_and_workspace(client, db_sessionmaker, "supervisor-redis@example.com")
    docker_client = FakeDockerClient()
    redis = FakeRedis()
    presence = PresenceService(redis_client=redis)
    presence.heartbeat(user_id, "tab-1")
    _run_supervisor_once(db_sessionmaker, presence, docker_client)
    container = docker_client.containers.get(f"notepatch-openclaw-{user_id}")

    broken_presence = PresenceService(redis_client=FakeRedis(fail_reads=True))
    _run_supervisor_once(db_sessionmaker, broken_presence, docker_client)

    assert container.status == "running"
    assert container.stopped == 0
