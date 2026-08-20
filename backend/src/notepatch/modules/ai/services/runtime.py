from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from notepatch.modules.ai.services.runtime_config import NOTEPATCH_SKILLS, OpenClawRuntimeConfig
from notepatch.modules.ai.services.runtime_mirror import OpenClawRuntimeMirror
from notepatch.modules.ai.services.runtime_paths import OpenClawRuntimePaths
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.platform.config import get_settings


class OpenClawUserRuntimeError(RuntimeError):
    pass


class OpenClawUserRuntimeService(
    OpenClawRuntimePaths,
    OpenClawRuntimeConfig,
    OpenClawRuntimeMirror,
):
    """Provision and expose one isolated OpenClaw runtime per personal workspace owner."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.root = Path(self.settings.openclaw_user_runtime_root).resolve()
        self.asset_root = self._resolve_asset_root()

    def _resolve_asset_root(self) -> Path:
        configured = Path(self.settings.openclaw_asset_root).expanduser()
        if configured.is_dir():
            return configured.resolve()
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "openclaw"
            if candidate.is_dir():
                return candidate
        raise OpenClawUserRuntimeError(
            f"OpenClaw asset root does not exist: {configured}. "
            "Set OPENCLAW_ASSET_ROOT to the monorepo openclaw directory."
        )

    def provision_user(
        self, user: User, workspace: Workspace, *, model_ids: tuple[str, ...] | None = None
    ) -> dict:
        user_root = self.user_root(user.id)
        existing_runtime = user_root.exists()
        shutil.rmtree(self.documents_root(user.id), ignore_errors=True)
        runtime_directories = (
            self.home_dir(user.id) / ".openclaw",
            self.auth_profiles_path(user.id).parent,
            self.skills_dir(user.id),
            self.workspace_dir(user.id),
            self.notepatch_root(user.id) / "openclaw" / "tasks",
            self.cache_dir(user.id),
            self.tmp_dir(user.id),
        )
        for path in runtime_directories:
            path.mkdir(parents=True, exist_ok=True)

        token = self._ensure_env(user.id)
        self._ensure_openclaw_json(
            user.id, user=user, workspace=workspace, model_ids=model_ids
        )
        self._ensure_auth_profiles(user.id)
        self._ensure_notepatch_skills(user.id)
        self._write_notepatch_runtime(user.id, user=user, workspace=workspace)
        self._write_compose(user.id)
        if existing_runtime:
            self._ensure_runtime_entry_permissions(
                (
                    user_root,
                    *runtime_directories,
                    self.env_path(user.id),
                    self.openclaw_json_path(user.id),
                    self.auth_profiles_path(user.id),
                    self.notepatch_runtime_path(user.id),
                    self.compose_path(user.id),
                    *(
                        path
                        for skill_id in NOTEPATCH_SKILLS
                        for path in (
                            self.skills_dir(user.id) / skill_id,
                            self.skills_dir(user.id) / skill_id / "SKILL.md",
                        )
                    ),
                )
            )
        else:
            self._ensure_runtime_permissions(user.id)
        return {
            "user_root": str(user_root),
            "home_dir": str(self.home_dir(user.id)),
            "workspace_dir": str(self.workspace_dir(user.id)),
            "container_name": self.container_name(user.id),
            "gateway_url": self.gateway_url(user.id),
            "gateway_token": token,
        }

    def runtime_for_workspace(
        self, db: Session, workspace_id: str, *, model_ids: tuple[str, ...] | None = None
    ) -> dict:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            raise OpenClawUserRuntimeError("Workspace not found")
        user = db.get(User, workspace.owner_user_id)
        if user is None:
            raise OpenClawUserRuntimeError("Workspace owner not found")
        runtime = self.provision_user(user, workspace, model_ids=model_ids)
        runtime["user_id"] = user.id
        runtime["workspace_id"] = workspace.id
        return runtime

    def collect_task_outputs(self, user_id: str, task_id: str) -> list[Path]:
        output_dir = self.task_output_dir(user_id, task_id)
        if not output_dir.exists():
            return []
        return [path for path in output_dir.rglob("*") if path.is_file()]

    def cleanup_task_context(self, db: Session, workspace_id: str, task_id: str) -> None:
        workspace = db.get(Workspace, workspace_id)
        if workspace is None:
            return
        task_root = self.task_input_dir(workspace.owner_user_id, task_id).parent
        expected_parent = self.notepatch_root(workspace.owner_user_id) / "openclaw" / "tasks"
        if task_root.parent.resolve() != expected_parent.resolve():
            raise OpenClawUserRuntimeError("Task context path escaped the user runtime")
        shutil.rmtree(task_root, ignore_errors=True)


__all__ = ["NOTEPATCH_SKILLS", "OpenClawUserRuntimeError", "OpenClawUserRuntimeService"]
