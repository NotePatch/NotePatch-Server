from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
from pathlib import Path
import shutil
import tempfile
import uuid

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from notepatch.modules.identity.models.user import (
    IdentityAuditLog,
    IdentityMutationKey,
    RefreshToken,
    User,
)
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.platform.security import verify_password
from notepatch.platform.storage import StorageService
from notepatch.shared.api import ApiError


logger = logging.getLogger(__name__)


def canonical_request_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def profile_etag(profile_version: int) -> str:
    return f'"profile-{profile_version}"'


def parse_profile_etag(value: str | None) -> int:
    if value is None:
        raise ApiError(428, "precondition_required", "If-Match is required")
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:]
    normalized = normalized.strip('"')
    if not normalized.startswith("profile-"):
        raise ApiError(400, "invalid_if_match", "If-Match must use the profile ETag")
    try:
        return int(normalized.removeprefix("profile-"))
    except ValueError as exc:
        raise ApiError(400, "invalid_if_match", "If-Match must use the profile ETag") from exc


def require_idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key must contain 8 to 128 characters")
    if any(ord(character) < 33 or ord(character) > 126 for character in normalized):
        raise ApiError(400, "invalid_idempotency_key", "Idempotency-Key contains invalid characters")
    return normalized


def _masked_email(email: str) -> str:
    local, _, domain = email.partition("@")
    visible = local[:1] if local else "*"
    return f"{visible}***@{domain}" if domain else "***"


class ProfileService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    @staticmethod
    def read(user: User, *, reauthentication_required: bool = False) -> dict:
        return {
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "avatar_url": user.avatar_url,
            "profile_version": user.profile_version,
            "reauthentication_required": reauthentication_required,
        }

    def update(
        self,
        user_id: str,
        *,
        fields: dict,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        request_context: dict,
    ) -> tuple[dict, bool]:
        replay = self._existing_mutation(user_id, "profile.update", idempotency_key, request_hash)
        if replay is not None:
            return replay, True
        user = self._locked_user(user_id)
        replay = self._existing_mutation(user_id, "profile.update", idempotency_key, request_hash)
        if replay is not None:
            return replay, True
        self._require_profile_version(user, expected_version)
        before = self._audit_snapshot(user)
        changed = False
        reauthentication_required = False

        if "name" in fields and fields["name"] != user.full_name:
            user.full_name = fields["name"]
            changed = True

        requested_email = fields.get("email")
        normalized_email = requested_email.lower().strip() if isinstance(requested_email, str) else None
        if normalized_email is not None and normalized_email != user.email:
            password = fields.get("current_password")
            if not isinstance(password, str) or not verify_password(password, user.password_hash):
                raise ApiError(403, "current_password_invalid", "Current password is required to change email")
            self._validate_email_change(user, normalized_email)
            user.email = normalized_email
            user.auth_version += 1
            self._revoke_refresh_tokens(user.id, "profile_email_change")
            reauthentication_required = True
            changed = True

        if changed:
            user.profile_version += 1
        response = self.read(user, reauthentication_required=reauthentication_required)
        self._audit(user, "profile.update", before, self._audit_snapshot(user), idempotency_key, request_context)
        self._complete_mutation(user.id, "profile.update", idempotency_key, request_hash, response)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            code = "email_conflict" if reauthentication_required else "identity_conflict"
            message = "Email is already registered" if reauthentication_required else "Profile update conflicted with another request"
            raise ApiError(409, code, message) from exc
        self.db.refresh(user)
        return response, False

    def begin_avatar_mutation(
        self,
        user_id: str,
        *,
        operation_scope: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[User | None, dict | None]:
        replay = self._existing_mutation(user_id, operation_scope, idempotency_key, request_hash)
        if replay is not None:
            return None, replay
        user = self._locked_user(user_id)
        replay = self._existing_mutation(user_id, operation_scope, idempotency_key, request_hash)
        if replay is not None:
            return None, replay
        self._require_profile_version(user, expected_version)
        return user, None

    def finish_avatar_mutation(
        self,
        user: User,
        *,
        operation_scope: str,
        idempotency_key: str,
        request_hash: str,
        before: dict,
        response: dict,
        request_context: dict,
    ) -> None:
        self._audit(user, operation_scope, before, self._audit_snapshot(user), idempotency_key, request_context)
        self._complete_mutation(user.id, operation_scope, idempotency_key, request_hash, response)
        try:
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise ApiError(409, "idempotency_conflict", "The mutation was already submitted") from exc
        self.db.refresh(user)

    @staticmethod
    def _audit_snapshot(user: User) -> dict:
        return {
            "name": user.full_name,
            "email": _masked_email(user.email),
            "avatar_url": user.avatar_url,
            "profile_version": user.profile_version,
        }

    def _locked_user(self, user_id: str) -> User:
        user = self.db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or not user.is_active:
            raise ApiError(401, "unauthorized", "User is not available")

        return user

    @staticmethod
    def _require_profile_version(user: User, expected_version: int) -> None:
        if user.profile_version != expected_version:
            raise ApiError(
                412,
                "profile_version_mismatch",
                "Profile has changed; fetch it again before retrying",
                data={"current_profile_version": user.profile_version},
                headers={"ETag": profile_etag(user.profile_version)},
            )

    def _existing_mutation(
        self,
        user_id: str,
        operation_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict | None:
        row = self.db.scalar(
            select(IdentityMutationKey).where(
                IdentityMutationKey.user_id == user_id,
                IdentityMutationKey.operation_scope == operation_scope,
                IdentityMutationKey.idempotency_key == idempotency_key,
            )
        )
        if row is None:
            return None
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
        if expires_at <= utcnow():
            self.db.delete(row)
            self.db.flush()
            return None
        if row.request_hash != request_hash:
            raise ApiError(409, "idempotency_conflict", "Idempotency-Key was already used for another request")
        if row.status != "completed" or row.response_data is None:
            raise ApiError(409, "request_in_progress", "An identical request is still being processed")
        return dict(row.response_data)

    def _complete_mutation(
        self,
        user_id: str,
        operation_scope: str,
        idempotency_key: str,
        request_hash: str,
        response: dict,
    ) -> None:
        self.db.add(
            IdentityMutationKey(
                user_id=user_id,
                operation_scope=operation_scope,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="completed",
                response_data=response,
                expires_at=utcnow() + timedelta(seconds=self.settings.identity_idempotency_ttl_seconds),
            )
        )

    def _audit(
        self,
        user: User,
        action: str,
        before: dict,
        after: dict,
        idempotency_key: str,
        request_context: dict,
    ) -> None:
        self.db.add(
            IdentityAuditLog(
                actor_user_id=user.id,
                action=action,
                request_id=request_context.get("request_id"),
                idempotency_key_hash=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
                ip_address=request_context.get("ip_address"),
                user_agent=(request_context.get("user_agent") or "")[:512] or None,
                before_data=before,
                after_data=after,
                result="succeeded",
            )
        )

    def _validate_email_change(self, user: User, email: str) -> None:
        duplicate = self.db.scalar(select(User.id).where(User.email == email, User.id != user.id))
        if duplicate is not None:
            raise ApiError(409, "email_conflict", "Email is already registered")
        admin_emails = self.settings.admin_email_set
        was_admin = user.email.lower() in admin_emails
        will_be_admin = email in admin_emails
        if will_be_admin and not was_admin:
            raise ApiError(403, "reserved_email", "This email is reserved by the deployment administrator")
        if was_admin and not will_be_admin:
            active_admins = int(
                self.db.scalar(
                    select(func.count(User.id)).where(
                        User.is_active.is_(True),
                        func.lower(User.email).in_(admin_emails),
                    )
                )
                or 0
            )
            if active_admins <= 1:
                raise ApiError(409, "last_admin", "The last active administrator cannot change to a non-admin email")

    def _revoke_refresh_tokens(self, user_id: str, reason: str) -> None:
        now = utcnow()
        for token in self.db.scalars(
            select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        ).all():
            token.revoked_at = now
            token.revoked_reason = reason


class AvatarService:
    def __init__(self, db: Session, storage: StorageService) -> None:
        self.db = db
        self.storage = storage
        self.settings = get_settings()
        if self.settings.avatar_storage_backend not in {"seaweedfs", "local"}:
            raise RuntimeError("AVATAR_STORAGE_BACKEND must be seaweedfs or local")

    def normalize(self, content: bytes) -> tuple[bytes, str, str]:
        maximum = self.settings.user_avatar_max_size_mb * 1024 * 1024
        if not content:
            raise ApiError(422, "avatar_invalid", "Avatar file is empty")
        if len(content) > maximum:
            raise ApiError(413, "avatar_too_large", f"Avatar exceeds {self.settings.user_avatar_max_size_mb} MB")
        try:
            from io import BytesIO

            source = BytesIO(content)
            with Image.open(source) as image:
                image.load()
                if image.format not in {"JPEG", "PNG"}:
                    raise ApiError(422, "avatar_invalid", "Avatar must be a JPEG or PNG image")
                if max(image.size) > self.settings.user_avatar_max_dimension:
                    raise ApiError(422, "avatar_dimensions_exceeded", "Avatar dimensions are too large")
                output = BytesIO()
                if image.format == "JPEG":
                    image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                    return output.getvalue(), "image/jpeg", "jpg"
                normalized = image.convert("RGBA") if image.mode in {"RGBA", "LA", "P"} else image.convert("RGB")
                normalized.save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png", "png"
        except ApiError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise ApiError(422, "avatar_invalid", "Avatar is not a valid JPEG or PNG image") from exc

    def upload(
        self,
        user_id: str,
        *,
        normalized: bytes,
        mime_type: str,
        extension: str,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        request_context: dict,
    ) -> tuple[dict, bool]:
        profiles = ProfileService(self.db)
        user, replay = profiles.begin_avatar_mutation(
            user_id,
            operation_scope="avatar.upload",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay, True
        assert user is not None
        before = profiles._audit_snapshot(user)
        old_backend, old_key = user.avatar_storage_backend, user.avatar_object_key
        version = str(uuid.uuid4())
        object_key = StorageService.user_avatar_key(user.id, version, extension)
        self._write(object_key, normalized, mime_type)
        try:
            user.avatar_storage_backend = self.settings.avatar_storage_backend
            user.avatar_object_key = object_key
            user.avatar_mime_type = mime_type
            user.avatar_size = len(normalized)
            user.profile_version += 1
            user.avatar_url = f"/api/v1/user/avatar/content?v={version}"
            response = self.read(user)
            profiles.finish_avatar_mutation(
                user,
                operation_scope="avatar.upload",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                before=before,
                response=response,
                request_context=request_context,
            )
        except Exception:
            if not self._delete(self.settings.avatar_storage_backend, object_key, suppress=True):
                self._schedule_cleanup(user.id, self.settings.avatar_storage_backend, object_key)
            raise
        if old_backend and old_key and old_key != object_key:
            if not self._delete(old_backend, old_key, suppress=True):
                self._schedule_cleanup(user.id, old_backend, old_key)
        return response, False

    def delete(
        self,
        user_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        request_hash: str,
        request_context: dict,
    ) -> tuple[dict, bool]:
        profiles = ProfileService(self.db)
        user, replay = profiles.begin_avatar_mutation(
            user_id,
            operation_scope="avatar.delete",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay, True
        assert user is not None
        before = profiles._audit_snapshot(user)
        old_backend, old_key = user.avatar_storage_backend, user.avatar_object_key
        user.profile_version += 1
        user.avatar_url = None
        user.avatar_storage_backend = None
        user.avatar_object_key = None
        user.avatar_mime_type = None
        user.avatar_size = None
        response = self.read(user)
        profiles.finish_avatar_mutation(
            user,
            operation_scope="avatar.delete",
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            before=before,
            response=response,
            request_context=request_context,
        )
        if old_backend and old_key:
            if not self._delete(old_backend, old_key, suppress=True):
                self._schedule_cleanup(user.id, old_backend, old_key)
        return response, False

    @staticmethod
    def read(user: User) -> dict:
        return {
            "avatar_url": user.avatar_url,
            "mime_type": user.avatar_mime_type,
            "file_size": user.avatar_size,
            "profile_version": user.profile_version,
        }

    def download_url(self, user: User) -> str:
        if not user.avatar_object_key or not user.avatar_storage_backend:
            raise ApiError(404, "avatar_not_found", "Avatar has not been uploaded")
        if user.avatar_storage_backend == "seaweedfs":
            return self.storage.create_presigned_download_url(
                self.storage.bucket,
                user.avatar_object_key,
                self.settings.presign_expire_seconds,
            )
        return user.avatar_url or "/api/v1/user/avatar/content"

    def local_path(self, user: User) -> Path:
        if user.avatar_storage_backend != "local" or not user.avatar_object_key:
            raise ApiError(404, "avatar_not_found", "Avatar is not stored locally")
        path = self._safe_local_path(user.avatar_object_key)
        if not path.is_file():
            raise ApiError(404, "avatar_not_found", "Avatar object is unavailable")
        return path

    def purge_user_avatar(self, user: User) -> None:
        if user.avatar_storage_backend and user.avatar_object_key:
            self._delete(user.avatar_storage_backend, user.avatar_object_key, suppress=False)
        if self.settings.avatar_storage_backend == "seaweedfs":
            self.storage.delete_prefix(f"users/{user.id}/profile/avatar/")
            return
        local_user_root = self._safe_local_path(f"users/{user.id}/profile/avatar/placeholder").parent
        shutil.rmtree(local_user_root, ignore_errors=True)

    def cleanup_object(self, *, user_id: str, backend: str, object_key: str) -> None:
        expected_prefix = f"users/{user_id}/profile/avatar/"
        if not user_id or not object_key.startswith(expected_prefix):
            raise ValueError("Avatar cleanup object key is outside the user avatar prefix")
        if backend not in {"seaweedfs", "local"}:
            raise ValueError("Avatar cleanup storage backend is invalid")
        self._delete(backend, object_key, suppress=False)

    def _write(self, object_key: str, content: bytes, mime_type: str) -> None:
        try:
            if self.settings.avatar_storage_backend == "local":
                path = self._safe_local_path(object_key)
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                    temporary.write(content)
                    temporary_path = Path(temporary.name)
                temporary_path.replace(path)
                return
            with tempfile.TemporaryDirectory(prefix="notepatch-avatar-") as directory:
                path = Path(directory) / "avatar"
                path.write_bytes(content)
                self.storage.put_file(self.storage.bucket, object_key, path, content_type=mime_type)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(503, "storage_unavailable", "Avatar storage is unavailable") from exc

    def _delete(self, backend: str, object_key: str, *, suppress: bool) -> bool:
        try:
            if backend == "local":
                self._safe_local_path(object_key).unlink(missing_ok=True)
            else:
                self.storage.delete_object(self.storage.bucket, object_key)
            return True
        except Exception as exc:
            if not suppress:
                raise
            logger.warning("Could not remove obsolete avatar object %s: %s", object_key, exc)
            return False

    def _schedule_cleanup(self, user_id: str, backend: str, object_key: str) -> None:
        workspace_id = self.db.scalar(select(Workspace.id).where(Workspace.owner_user_id == user_id))
        if workspace_id is None:
            logger.warning("Could not schedule avatar cleanup for user %s without a workspace", user_id)
            return
        cleanup_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{backend}:{object_key}"))
        tasks = TaskService(self.db)
        existing = tasks.find_active_task(
            workspace_id=workspace_id,
            task_type="purge_avatar_object",
            resource_type="user_avatar_object",
            resource_id=cleanup_id,
        )
        if existing is not None:
            return
        try:
            tasks.create_task(
                workspace_id=workspace_id,
                task_type="purge_avatar_object",
                resource_type="user_avatar_object",
                resource_id=cleanup_id,
                payload={
                    "user_id": user_id,
                    "storage_backend": backend,
                    "object_key": object_key,
                },
            )
        except Exception as exc:
            self.db.rollback()
            logger.warning("Could not enqueue obsolete avatar cleanup %s: %s", object_key, exc)

    def _safe_local_path(self, object_key: str) -> Path:
        configured = self.settings.avatar_local_root
        root = Path(configured or Path(self.settings.notepatch_data_root) / "avatars").resolve()
        path = (root / object_key).resolve()
        if not path.is_relative_to(root):
            raise ApiError(400, "avatar_path_invalid", "Avatar storage path is invalid")
        return path
