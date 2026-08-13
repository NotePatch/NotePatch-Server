from __future__ import annotations

import hashlib
import json
import logging
import time
import shutil
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.platform.config import get_settings
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.identity.services.presence import PresenceService
from notepatch.modules.admin.models.admin import AdminOperation

logger = logging.getLogger(__name__)


MANAGED_LABELS = {
    "notepatch.managed": "true",
    "notepatch.kind": "openclaw-gateway",
}
OPENCLAW_TASK_TYPES = {
    "openclaw_agent_run",
    "extract_questions",
    "build_knowledge_base",
    "generate_study_notes",
    "generate_flashcards",
    "grade_homework",
    "highlight_study_notes",
}


class OpenClawSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenClawContainerSpec:
    user_id: str
    workspace_id: str
    name: str
    image: str
    network: str
    environment: dict[str, str]
    volumes: dict[str, dict[str, str]]
    group_add: list[str]
    command: list[str]
    labels: dict[str, str]
    config_hash: str


def docker_client_from_env():
    try:
        import docker
    except ImportError as exc:
        raise OpenClawSupervisorError("Python Docker SDK is not installed") from exc
    return docker.from_env()


class OpenClawContainerManager:
    def __init__(
        self,
        *,
        docker_client=None,
        runtime_service: OpenClawUserRuntimeService | None = None,
    ) -> None:
        self.settings = get_settings()
        self.docker = docker_client or docker_client_from_env()
        self.runtime = runtime_service or OpenClawUserRuntimeService()

    def ensure_running(self, user: User, workspace: Workspace):
        self.runtime.provision_user(user, workspace)
        spec = self.build_spec(user.id, workspace.id)
        container = self._get_container(spec.name)
        if container is not None and self._config_hash(container) != spec.config_hash:
            logger.info("Recreating OpenClaw gateway %s because runtime config changed", spec.name)
            container.reload()
            if getattr(container, "status", None) == "running":
                self._stop_container(container)
            container.remove()
            container = None

        if container is None:
            logger.info("Creating OpenClaw gateway container %s", spec.name)
            container = self.docker.containers.run(
                spec.image,
                command=spec.command,
                detach=True,
                name=spec.name,
                environment=spec.environment,
                volumes=spec.volumes,
                group_add=spec.group_add,
                labels=spec.labels,
                network=spec.network,
                restart_policy={"Name": "unless-stopped"},
                mem_limit=self.settings.openclaw_gateway_memory_limit,
                nano_cpus=self.settings.openclaw_gateway_nano_cpus,
                pids_limit=self.settings.openclaw_gateway_pids_limit,
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
            )
            return container

        container.reload()
        if getattr(container, "status", None) != "running":
            logger.info("Starting OpenClaw gateway container %s", spec.name)
            container.start()
        return container

    def stop_user(self, user_id: str) -> bool:
        container = self._get_container(self.runtime.container_name(user_id))
        if container is None:
            return False
        container.reload()
        if getattr(container, "status", None) == "running":
            self._stop_container(container)
            return True
        return False

    def remove_user(self, user_id: str) -> None:
        container = self._get_container(self.runtime.container_name(user_id))
        if container is not None:
            container.reload()
            if getattr(container, "status", None) == "running":
                self._stop_container(container)
            container.remove()
        shutil.rmtree(self.runtime.user_root(user_id), ignore_errors=True)

    def managed_containers(self) -> list:
        return self.docker.containers.list(
            all=True,
            filters={"label": [f"{key}={value}" for key, value in MANAGED_LABELS.items()]},
        )

    def build_spec(self, user_id: str, workspace_id: str) -> OpenClawContainerSpec:
        env = self.runtime.runtime_env(user_id)
        image = env.get("OPENCLAW_USER_GATEWAY_IMAGE") or self.settings.openclaw_user_gateway_image
        network = env.get("OPENCLAW_DOCKER_NETWORK") or self.settings.openclaw_docker_network
        command = ["sh", "-lc", self.runtime.gateway_command()]
        environment = self.runtime.container_environment(user_id)
        volumes = self.runtime.container_volumes(user_id)
        group_add = self.runtime.container_group_add()
        config_hash = self._runtime_config_hash(
            user_id=user_id,
            image=image,
            network=network,
            command=command,
            environment=environment,
            volumes=volumes,
            group_add=group_add,
        )
        labels = {
            **MANAGED_LABELS,
            "notepatch.user_id": user_id,
            "notepatch.workspace_id": workspace_id,
            "notepatch.config_hash": config_hash,
        }
        return OpenClawContainerSpec(
            user_id=user_id,
            workspace_id=workspace_id,
            name=self.runtime.container_name(user_id),
            image=image,
            network=network,
            environment=environment,
            volumes=volumes,
            group_add=group_add,
            command=command,
            labels=labels,
            config_hash=config_hash,
        )

    def _runtime_config_hash(
        self,
        *,
        user_id: str,
        image: str,
        network: str,
        command: list[str],
        environment: dict[str, str],
        volumes: dict[str, dict[str, str]],
        group_add: list[str],
    ) -> str:
        openclaw_config: object = ""
        config_path = self.runtime.openclaw_json_path(user_id)
        if config_path.exists():
            raw_config = config_path.read_text(encoding="utf-8")
            try:
                openclaw_config = self._deployment_openclaw_config(json.loads(raw_config))
            except json.JSONDecodeError:
                openclaw_config = raw_config
        payload = {
            "image": image,
            "network": network,
            "command": command,
            "environment": environment,
            "volumes": volumes,
            "group_add": group_add,
            "openclaw_json": openclaw_config,
            "resources": {
                "memory": self.settings.openclaw_gateway_memory_limit,
                "nano_cpus": self.settings.openclaw_gateway_nano_cpus,
                "pids_limit": self.settings.openclaw_gateway_pids_limit,
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _deployment_openclaw_config(config: object) -> object:
        """Exclude fields OpenClaw itself writes during a normal gateway startup."""
        if not isinstance(config, dict):
            return config
        normalized = json.loads(json.dumps(config))
        normalized.pop("meta", None)
        plugins = normalized.get("plugins")
        if isinstance(plugins, dict):
            entries = plugins.get("entries")
            if isinstance(entries, dict) and entries.get("openai") == {"enabled": True}:
                entries.pop("openai", None)
            if isinstance(entries, dict) and not entries:
                plugins.pop("entries", None)
            if not plugins:
                normalized.pop("plugins", None)
        return normalized

    def _get_container(self, name: str):
        try:
            return self.docker.containers.get(name)
        except Exception as exc:
            if exc.__class__.__name__ == "NotFound":
                return None
            raise

    def _stop_container(self, container) -> None:
        container.stop(timeout=self.settings.openclaw_supervisor_container_stop_timeout_seconds)

    @staticmethod
    def _config_hash(container) -> str | None:
        labels = getattr(container, "labels", None)
        if labels is None:
            attrs = getattr(container, "attrs", {}) or {}
            labels = ((attrs.get("Config") or {}).get("Labels") or {})
        return labels.get("notepatch.config_hash")


class OpenClawSupervisor:
    def __init__(
        self,
        *,
        db_factory,
        presence: PresenceService | None = None,
        container_manager: OpenClawContainerManager | None = None,
    ) -> None:
        self.settings = get_settings()
        self.db_factory = db_factory
        self.presence = presence or PresenceService()
        self.containers = container_manager or OpenClawContainerManager()

    def run_once(self) -> None:
        self._process_runtime_cleanup_requests()
        try:
            online_user_ids = self.presence.online_user_ids()
            tracked_user_ids = self.presence.tracked_user_ids()
        except Exception as exc:
            logger.warning("Could not read Redis presence; skipping OpenClaw lifecycle pass: %s", exc)
            return

        with self.db_factory() as db:
            active_task_user_ids = self._active_task_user_ids(db)
            for user_id in sorted(online_user_ids | active_task_user_ids):
                self._ensure_online_user(db, user_id)
            self._stop_offline_users(db, online_user_ids, tracked_user_ids)

    def _process_runtime_cleanup_requests(self) -> None:
        with self.db_factory() as db:
            operations = db.scalars(
                select(AdminOperation).where(
                    AdminOperation.operation_type == "purge_user",
                    AdminOperation.status.in_(("queued", "running")),
                    AdminOperation.phase == "runtime_cleanup_requested",
                )
            ).all()
            for operation in operations:
                try:
                    self.containers.remove_user(operation.target_id)
                    operation.phase = "runtime_cleanup_completed"
                    db.commit()
                except Exception as exc:
                    operation.error_message = f"OpenClaw runtime cleanup failed: {exc}"
                    db.commit()
                    logger.exception("Could not remove OpenClaw runtime for user %s", operation.target_id)

    def run_forever(self, should_stop=lambda: False) -> None:
        while not should_stop():
            self.run_once()
            time.sleep(self.settings.openclaw_supervisor_poll_seconds)

    def _ensure_online_user(self, db: Session, user_id: str) -> None:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return
        workspace = db.scalar(select(Workspace).where(Workspace.owner_user_id == user.id, Workspace.type == "personal"))
        if workspace is None:
            return
        try:
            self.containers.ensure_running(user, workspace)
        except Exception as exc:
            logger.exception("Could not ensure OpenClaw gateway for user %s: %s", user_id, exc)

    def _stop_offline_users(self, db: Session, online_user_ids: set[str], tracked_user_ids: set[str]) -> None:
        managed_user_ids = set(tracked_user_ids)
        try:
            managed_containers = self.containers.managed_containers()
        except Exception as exc:
            logger.warning("Could not list OpenClaw containers; skipping offline stop pass: %s", exc)
            return
        for container in managed_containers:
            user_id = self._container_user_id(container)
            if user_id:
                managed_user_ids.add(user_id)

        now = time.time()
        for user_id in sorted(managed_user_ids - online_user_ids):
            if self._has_active_openclaw_task(db, user_id):
                continue
            last_seen = self.presence.last_seen_epoch(user_id)
            if last_seen is not None and now - last_seen < self.settings.presence_offline_grace_seconds:
                continue
            try:
                self.containers.stop_user(user_id)
            except Exception as exc:
                logger.exception("Could not stop OpenClaw gateway for offline user %s: %s", user_id, exc)

    def _has_active_openclaw_task(self, db: Session, user_id: str) -> bool:
        workspace = db.scalar(select(Workspace).where(Workspace.owner_user_id == user_id, Workspace.type == "personal"))
        if workspace is None:
            return False
        task_id = db.scalar(
            select(Task.id)
            .where(
                Task.workspace_id == workspace.id,
                Task.task_type.in_(OPENCLAW_TASK_TYPES),
                Task.status.in_(("queued", "running")),
            )
            .limit(1)
        )
        return task_id is not None

    def _active_task_user_ids(self, db: Session) -> set[str]:
        rows = db.execute(
            select(Workspace.owner_user_id)
            .join(Task, Task.workspace_id == Workspace.id)
            .where(
                Workspace.type == "personal",
                Task.task_type.in_(OPENCLAW_TASK_TYPES),
                Task.status.in_(("queued", "running")),
            )
            .distinct()
        ).all()
        return {user_id for (user_id,) in rows if isinstance(user_id, str)}

    @staticmethod
    def _container_user_id(container) -> str | None:
        labels = getattr(container, "labels", None)
        if labels is None:
            attrs = getattr(container, "attrs", {}) or {}
            labels = ((attrs.get("Config") or {}).get("Labels") or {})
        value = labels.get("notepatch.user_id")
        return value if isinstance(value, str) and value else None
