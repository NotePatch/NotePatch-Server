from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable


SAFE_ID = re.compile(r"[^a-zA-Z0-9_-]+")


class OpenClawRuntimePaths:
    settings: object
    root: Path

    def safe_user_id(self, user_id: str) -> str:
        from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeError

        safe = SAFE_ID.sub("-", user_id).strip("-")
        if not safe:
            raise OpenClawUserRuntimeError("User id cannot be converted to a safe OpenClaw runtime id")
        return safe

    def user_root(self, user_id: str) -> Path:
        return self.root / "users" / self.safe_user_id(user_id)

    def home_dir(self, user_id: str) -> Path:
        return self.user_root(user_id) / "home"

    def workspace_dir(self, user_id: str) -> Path:
        return self.user_root(user_id) / "workspace"

    def cache_dir(self, user_id: str) -> Path:
        return self.user_root(user_id) / "cache"

    def tmp_dir(self, user_id: str) -> Path:
        return self.user_root(user_id) / "tmp"

    def container_name(self, user_id: str) -> str:
        return f"notepatch-openclaw-{self.safe_user_id(user_id)}"

    def gateway_url(self, user_id: str) -> str:
        return f"http://{self.container_name(user_id)}:18789"

    def gateway_command(self) -> str:
        return (
            "corepack enable >/dev/null 2>&1 || true; "
            'node /app/dist/index.js gateway run --allow-unconfigured --auth token --token "$OPENCLAW_GATEWAY_TOKEN" '
            "--bind lan --port 18789"
        )

    def openclaw_json_path(self, user_id: str) -> Path:
        return self.home_dir(user_id) / ".openclaw" / "openclaw.json"

    def notepatch_runtime_path(self, user_id: str) -> Path:
        return self.user_root(user_id) / "notepatch-runtime.json"

    def auth_profiles_path(self, user_id: str) -> Path:
        return self.home_dir(user_id) / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"

    def skills_dir(self, user_id: str) -> Path:
        # Workspace skills are visible inside an OpenClaw sandbox as
        # /workspace/skills. Agent-level skills under ~/.openclaw are outside
        # the sandbox root when workspace-only filesystem access is enabled.
        return self.workspace_dir(user_id) / "skills"

    def legacy_skills_dir(self, user_id: str) -> Path:
        return self.home_dir(user_id) / ".openclaw" / "skills"

    def env_path(self, user_id: str) -> Path:
        return self.user_root(user_id) / ".env"

    def compose_path(self, user_id: str) -> Path:
        return self.user_root(user_id) / "docker-compose.yml"

    def notepatch_root(self, user_id: str) -> Path:
        return self.workspace_dir(user_id) / "notepatch"

    def documents_root(self, user_id: str) -> Path:
        return self.notepatch_root(user_id) / "documents"

    def task_documents_root(self, user_id: str, task_id: str) -> Path:
        return self.task_input_dir(user_id, task_id) / "documents"

    def task_output_dir(self, user_id: str, task_id: str) -> Path:
        return self.notepatch_root(user_id) / "openclaw" / "tasks" / task_id / "output"

    def task_input_dir(self, user_id: str, task_id: str) -> Path:
        return self.notepatch_root(user_id) / "openclaw" / "tasks" / task_id / "input"

    def runtime_env(self, user_id: str) -> dict[str, str]:
        return self._read_env(self.env_path(user_id))

    def container_environment(self, user_id: str) -> dict[str, str]:
        env = self.runtime_env(user_id)
        values = {
            "HOME": "/home/node",
            "OPENCLAW_SKIP_CHANNELS": "1",
            "OPENCLAW_GATEWAY_TOKEN": env["OPENCLAW_GATEWAY_TOKEN"],
        }
        values.update(self._provider_environment())
        return values

    def container_volumes(self, user_id: str) -> dict[str, dict[str, str]]:
        return {
            str(self.home_dir(user_id)): {"bind": "/home/node", "mode": "rw"},
            str(self.cache_dir(user_id)): {"bind": "/home/node/.openclaw/cache", "mode": "rw"},
            str(self.tmp_dir(user_id)): {"bind": "/tmp/openclaw-runtime", "mode": "rw"},
            str(self.workspace_dir(user_id)): {"bind": str(self.workspace_dir(user_id)), "mode": "rw"},
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
        }

    def container_group_add(self) -> list[str]:
        gid = self.docker_socket_gid()
        return [str(gid)] if gid is not None else []

    def docker_socket_gid(self) -> int | None:
        configured = self.settings.openclaw_docker_socket_gid
        if configured is not None:
            return configured
        try:
            return os.stat("/var/run/docker.sock").st_gid
        except OSError:
            return None

    def _ensure_runtime_permissions(self, user_id: str, *, roots: Iterable[Path] | None = None) -> None:
        selected_roots = tuple(
            roots
            or (
                self.home_dir(user_id),
                self.workspace_dir(user_id),
                self.cache_dir(user_id),
                self.tmp_dir(user_id),
            )
        )
        for root in selected_roots:
            self._chown_tree(root)

    def _chown_tree(self, root: Path) -> None:
        if not root.exists():
            return
        for path in (root, *root.rglob("*")):
            try:
                os.chown(
                    path,
                    self.settings.openclaw_user_runtime_uid,
                    self.settings.openclaw_user_runtime_gid,
                    follow_symlinks=False,
                )
            except (AttributeError, PermissionError, OSError):
                continue

    def _provider_environment(self) -> dict[str, str]:
        values = {
            "OPENAI_API_KEY": self.settings.openai_api_key,
            "OPENAI_BASE_URL": self.settings.openai_base_url,
            "OPENAI_ORGANIZATION": self.settings.openai_organization,
            "OPENAI_PROJECT": self.settings.openai_project,
        }
        return {key: value for key, value in values.items() if value}

    @staticmethod
    def _read_env(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
        return values

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _write_json_if_changed(path: Path, payload: dict, *, mode: int | None = None) -> None:
        OpenClawRuntimePaths._write_text_if_changed(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=mode,
        )

    @staticmethod
    def _write_text_if_changed(path: Path, content: str, *, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
        if mode is not None:
            try:
                path.chmod(mode)
            except OSError:
                pass
