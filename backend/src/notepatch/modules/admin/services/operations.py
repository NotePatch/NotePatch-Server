from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

import redis
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.admin.models.admin import AdminAuditLog, AdminOperation
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.modules.identity.models.user import RefreshToken, User
from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from notepatch.modules.identity.services.permissions import get_role, seed_roles_and_permissions
from notepatch.modules.identity.services.presence import PresenceService
from notepatch.modules.identity.services.profile import AvatarService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.platform.errors import RetryableTaskError
from notepatch.platform.security import hash_password
from notepatch.platform.storage import StorageService


def user_snapshot(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "username": user.username,
        "phone": user.phone,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "ai_history_enabled": user.ai_history_enabled,
    }


class AdminOperationsService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def audit(
        self,
        actor: User,
        action: str,
        target_type: str,
        target_id: str,
        *,
        workspace_id: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        commit: bool = True,
    ) -> AdminAuditLog:
        row = AdminAuditLog(
            actor_user_id=actor.id,
            actor_email=actor.email,
            action=action,
            target_type=target_type,
            target_id=target_id,
            workspace_id=workspace_id,
            before_data=before,
            after_data=after,
        )
        self.db.add(row)
        if commit:
            self.db.commit()
        return row

    def create_user(
        self,
        actor: User,
        *,
        email: str,
        full_name: str | None,
        username: str | None,
        phone: str | None,
    ) -> tuple[User, str]:
        normalized = email.lower().strip()
        if self.db.scalar(select(User.id).where(User.email == normalized)) is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
        for field, value in (("username", username), ("phone", phone)):
            if value and self.db.scalar(select(User.id).where(getattr(User, field) == value)) is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{field} already registered")
        temporary_password = secrets.token_urlsafe(15)
        seed_roles_and_permissions(self.db)
        user = User(
            email=normalized,
            password_hash=hash_password(temporary_password),
            full_name=full_name,
            username=username,
            phone=phone,
            must_change_password=True,
        )
        self.db.add(user)
        self.db.flush()
        workspace = Workspace(name=f"{full_name or normalized}'s Workspace", type="personal", owner_user_id=user.id)
        self.db.add(workspace)
        self.db.flush()
        self.db.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=user.id,
                role_id=get_role(self.db, "owner").id,
            )
        )
        OpenClawUserRuntimeService().provision_user(user, workspace)
        self.audit(actor, "user.create", "user", user.id, workspace_id=workspace.id, after=user_snapshot(user), commit=False)
        self.db.commit()
        self.db.refresh(user)
        return user, temporary_password

    def update_user(self, actor: User, target: User, fields: dict) -> User:
        before = user_snapshot(target)
        old_email = target.email
        if target.id == actor.id and fields.get("is_active") is False:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Administrators cannot disable themselves")
        if fields.get("is_active") is False:
            self._protect_last_admin(target)
        for name in ("email", "username", "phone"):
            value = fields.get(name)
            if isinstance(value, str):
                normalized = value.lower().strip() if name == "email" else value
                duplicate = self.db.scalar(
                    select(User.id).where(getattr(User, name) == normalized, User.id != target.id)
                )
                if duplicate is not None:
                    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{name} already registered")
        for name in ("email", "full_name", "username", "phone", "is_active", "ai_history_enabled"):
            if name in fields:
                value = fields[name]
                if name == "email" and isinstance(value, str):
                    value = value.lower().strip()
                setattr(target, name, value)
        profile_fields = {"email", "full_name", "username", "phone"}
        if profile_fields.intersection(fields):
            target.profile_version += 1
        if target.email != old_email:
            target.auth_version += 1
            self._revoke_tokens(target.id)
        if fields.get("is_active") is False:
            self._revoke_tokens(target.id)
            self._clear_presence(target.id)
        self.audit(actor, "user.update", "user", target.id, before=before, after=user_snapshot(target), commit=False)
        self.db.commit()
        self.db.refresh(target)
        return target

    def reset_password(self, actor: User, target: User) -> str:
        temporary_password = secrets.token_urlsafe(15)
        target.password_hash = hash_password(temporary_password)
        target.must_change_password = True
        target.auth_version += 1
        self._revoke_tokens(target.id)
        self.audit(
            actor,
            "user.reset_password",
            "user",
            target.id,
            before={"must_change_password": False},
            after={"must_change_password": True},
            commit=False,
        )
        self.db.commit()
        return temporary_password

    def request_user_purge(self, actor: User, target: User) -> AdminOperation:
        if actor.id == target.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Administrators cannot delete themselves")
        self._protect_last_admin(target)
        workspace = self.db.scalar(select(Workspace).where(Workspace.owner_user_id == target.id))
        actor_workspace = self.db.scalar(select(Workspace).where(Workspace.owner_user_id == actor.id))
        if actor_workspace is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Administrator workspace not found")
        existing = self.db.scalar(
            select(AdminOperation).where(
                AdminOperation.operation_type == "purge_user",
                AdminOperation.target_id == target.id,
                AdminOperation.status.in_(("queued", "running")),
            )
        )
        if existing is not None:
            return existing
        before = user_snapshot(target)
        target.is_active = False
        target.must_change_password = True
        self._revoke_tokens(target.id)
        self._clear_presence(target.id)
        operation = AdminOperation(
            actor_user_id=actor.id,
            actor_workspace_id=actor_workspace.id,
            operation_type="purge_user",
            target_type="user",
            target_id=target.id,
            target_label_hash=hashlib.sha256(target.email.encode("utf-8")).hexdigest(),
            status="queued",
            phase="cancel_tasks",
            payload={"target_workspace_id": workspace.id if workspace else None},
        )
        self.db.add(operation)
        self.db.flush()
        task, queue_name = TaskService(self.db).create_task_record(
            workspace_id=actor_workspace.id,
            task_type="purge_user",
            resource_type="admin_operation",
            resource_id=operation.id,
            payload={"admin_operation_id": operation.id, "target_user_id": target.id},
            max_attempts=self.settings.purge_task_max_attempts,
        )
        operation.task_id = task.id
        self.audit(
            actor,
            "user.purge_requested",
            "user",
            target.id,
            workspace_id=workspace.id if workspace else None,
            before=before,
            after={"is_active": False, "operation_id": operation.id},
            commit=False,
        )
        self.db.commit()
        if not TaskService(self.db).enqueue_task(task.id, queue_name=queue_name):
            operation.status = "failed"
            operation.error_message = "Task queue is unavailable"
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Task queue is unavailable")
        return operation

    def _protect_last_admin(self, target: User) -> None:
        if target.email.lower() not in self.settings.admin_email_set:
            return
        active_admins = int(
            sum(
                1
                for user in self.db.scalars(select(User).where(User.is_active.is_(True))).all()
                if user.email.lower() in self.settings.admin_email_set
            )
        )
        if active_admins <= 1:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cannot disable or delete the last administrator")

    def _revoke_tokens(self, user_id: str) -> None:
        now = utcnow()
        for token in self.db.scalars(
            select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        ).all():
            token.revoked_at = now
            token.revoked_reason = "admin_action"

    def _clear_presence(self, user_id: str) -> None:
        try:
            client = redis.from_url(self.settings.redis_url, decode_responses=True)
            keys = list(client.scan_iter(match=f"{PresenceService.session_prefix}{user_id}:session:*"))
            if keys:
                client.delete(*keys)
            client.zrem(PresenceService.last_seen_key, user_id)
        except Exception:
            pass


class UserPurgeExecutor:
    def __init__(self, db: Session, storage: StorageService) -> None:
        self.db = db
        self.storage = storage
        self.settings = get_settings()

    def execute(self, task: Task) -> dict:
        operation_id = task.payload.get("admin_operation_id")
        target_user_id = task.payload.get("target_user_id")
        operation = self.db.get(AdminOperation, operation_id)
        if operation is None:
            raise RuntimeError("Admin operation not found")
        operation.status = "running"
        operation.error_message = None
        user = self.db.get(User, target_user_id)
        if user is None:
            return self._finish(operation, {"user_id": target_user_id, "already_absent": True})
        workspace = self.db.scalar(select(Workspace).where(Workspace.owner_user_id == user.id))
        if workspace is not None:
            waiting = []
            task_service = TaskService(self.db)
            for related in self.db.scalars(
                select(Task).where(
                    Task.workspace_id == workspace.id,
                    Task.status.in_(("queued", "running")),
                )
            ).all():
                task_service.request_cancel(related, "User deletion requested", commit=False)
                if related.status == "running":
                    waiting.append(related.id)
            if operation.phase != "runtime_cleanup_completed":
                operation.phase = "cancel_tasks"
            self.db.commit()
            if waiting:
                raise RetryableTaskError("Waiting for user tasks to cancel")

        if operation.phase != "runtime_cleanup_completed":
            operation.phase = "runtime_cleanup_requested"
            self.db.commit()
            raise RetryableTaskError("Waiting for OpenClaw runtime cleanup")

        if workspace is not None:
            operation.phase = "delete_external_data"
            self.db.commit()
            self.storage.delete_prefix(f"workspaces/{workspace.id}/")
            for upload in self.db.scalars(select(UploadSession).where(UploadSession.workspace_id == workspace.id)).all():
                if upload.tus_upload_id:
                    for suffix in ("", ".info"):
                        Path(self.settings.tusd_data_dir, f"{upload.tus_upload_id}{suffix}").unlink(missing_ok=True)
            operation.phase = "delete_database_data"
            self.db.commit()
            self.db.delete(workspace)
            self.db.flush()
        AvatarService(self.db, self.storage).purge_user_avatar(user)
        self.db.delete(user)
        self.db.commit()
        return self._finish(operation, {"user_id": target_user_id, "purged": True})

    def _finish(self, operation: AdminOperation, result: dict) -> dict:
        operation.status = "succeeded"
        operation.phase = "completed"
        operation.result = result
        operation.finished_at = utcnow()
        self.db.commit()
        return result
