import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_storage_service
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.schemas.profile import AvatarDownloadRead, AvatarRead, ProfileRead, ProfileUpdateRequest
from notepatch.modules.identity.services.profile import (
    AvatarService,
    ProfileService,
    canonical_request_hash,
    parse_profile_etag,
    profile_etag,
    require_idempotency_key,
)
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db
from notepatch.platform.storage import StorageService
from notepatch.shared.api import ApiEnvelope


router = APIRouter(prefix="/user", tags=["user-profile"])


def _request_context(request: Request) -> dict:
    return {
        "request_id": request.headers.get("X-Request-ID") or str(uuid.uuid4()),
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("User-Agent"),
    }


def _set_mutation_headers(response: Response, profile_version: int, replayed: bool) -> None:
    response.headers["ETag"] = profile_etag(profile_version)
    response.headers["Idempotent-Replayed"] = "true" if replayed else "false"


@router.get("/profile", response_model=ApiEnvelope[ProfileRead])
def get_profile(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> ApiEnvelope[ProfileRead]:
    response.headers["ETag"] = profile_etag(current_user.profile_version)
    return ApiEnvelope(code="ok", message="Profile loaded", data=ProfileService.read(current_user))


@router.put("/profile", response_model=ApiEnvelope[ProfileRead])
def update_profile(
    payload: ProfileUpdateRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApiEnvelope[ProfileRead]:
    idempotency_key = require_idempotency_key(idempotency_header)
    fields = payload.model_dump(exclude_unset=True)
    hash_payload = dict(fields)
    if isinstance(hash_payload.get("current_password"), str):
        hash_payload["current_password"] = hashlib.sha256(
            hash_payload["current_password"].encode("utf-8")
        ).hexdigest()
    data, replayed = ProfileService(db).update(
        current_user.id,
        fields=fields,
        expected_version=parse_profile_etag(if_match),
        idempotency_key=idempotency_key,
        request_hash=canonical_request_hash(hash_payload),
        request_context=_request_context(request),
    )
    _set_mutation_headers(response, data["profile_version"], replayed)
    return ApiEnvelope(code="ok", message="Profile updated", data=data)


@router.post("/avatar/upload", response_model=ApiEnvelope[AvatarRead])
async def upload_avatar(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> ApiEnvelope[AvatarRead]:
    idempotency_key = require_idempotency_key(idempotency_header)
    maximum = get_settings().user_avatar_max_size_mb * 1024 * 1024
    content = await file.read(maximum + 1)
    avatar_service = AvatarService(db, storage)
    normalized, mime_type, extension = avatar_service.normalize(content)
    request_hash = hashlib.sha256(normalized).hexdigest()
    data, replayed = avatar_service.upload(
        current_user.id,
        normalized=normalized,
        mime_type=mime_type,
        extension=extension,
        expected_version=parse_profile_etag(if_match),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        request_context=_request_context(request),
    )
    _set_mutation_headers(response, data["profile_version"], replayed)
    return ApiEnvelope(code="ok", message="Avatar uploaded", data=data)


@router.get("/avatar/download-url", response_model=ApiEnvelope[AvatarDownloadRead])
def get_avatar_download_url(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> ApiEnvelope[AvatarDownloadRead]:
    service = AvatarService(db, storage)
    data = {
        **service.read(current_user),
        "download_url": service.download_url(current_user),
        "expires_in": get_settings().presign_expire_seconds,
    }
    return ApiEnvelope(code="ok", message="Avatar download URL created", data=data)


@router.get("/avatar/content")
def get_avatar_content(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
):
    service = AvatarService(db, storage)
    if current_user.avatar_storage_backend == "local":
        path = service.local_path(current_user)
        return FileResponse(
            path,
            media_type=current_user.avatar_mime_type or "application/octet-stream",
            headers={"Cache-Control": "private, max-age=300"},
        )
    return RedirectResponse(service.download_url(current_user), status_code=307)


@router.delete("/avatar", response_model=ApiEnvelope[AvatarRead])
def delete_avatar(
    request: Request,
    response: Response,
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> ApiEnvelope[AvatarRead]:
    idempotency_key = require_idempotency_key(idempotency_header)
    data, replayed = AvatarService(db, storage).delete(
        current_user.id,
        expected_version=parse_profile_etag(if_match),
        idempotency_key=idempotency_key,
        request_hash=canonical_request_hash({"operation": "avatar.delete"}),
        request_context=_request_context(request),
    )
    _set_mutation_headers(response, data["profile_version"], replayed)
    return ApiEnvelope(code="ok", message="Avatar deleted", data=data)
