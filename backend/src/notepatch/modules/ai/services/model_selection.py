from __future__ import annotations

from sqlalchemy.orm import Session

from notepatch.modules.ai.services.model_catalog import normalize_ai_model_id
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.tasks.models.task import Task
from notepatch.platform.config import get_settings
from notepatch.platform.errors import PermanentTaskError


class AiModelSelectionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    @property
    def default_model(self) -> str:
        try:
            return normalize_ai_model_id(self.settings.openclaw_agent_model)
        except ValueError as exc:
            raise PermanentTaskError("Configured OpenClaw agent model is invalid") from exc

    def selected_for_user(self, user: User) -> str:
        configured = user.preferred_ai_model or self.default_model
        try:
            return normalize_ai_model_id(configured)
        except ValueError as exc:
            raise PermanentTaskError("Selected AI model is invalid") from exc

    def ensure_credentials(self, model_id: str) -> None:
        if model_id.startswith("openai/") and not self.settings.openai_api_key:
            raise PermanentTaskError("OPENAI_API_KEY is required by the selected AI model")

    def resolve_for_task(self, task: Task) -> tuple[str, bool]:
        payload = dict(task.payload or {})
        existing = payload.get("ai_model")
        if isinstance(existing, str) and existing.strip():
            try:
                return normalize_ai_model_id(existing), False
            except ValueError as exc:
                raise PermanentTaskError("Task AI model snapshot is invalid") from exc

        workspace = self.db.get(Workspace, task.workspace_id)
        if workspace is None or workspace.type != "personal":
            raise PermanentTaskError("Task personal workspace is unavailable")
        user = self.db.get(User, workspace.owner_user_id)
        if user is None or not user.is_active:
            raise PermanentTaskError("Task workspace owner is unavailable")
        selected = self.selected_for_user(user)
        payload["ai_model"] = selected
        task.payload = payload
        self.db.flush()
        return selected, True
