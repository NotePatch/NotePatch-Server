from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db
from notepatch.modules.documents.models.document import Document
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.platform.storage import StorageService
from notepatch.modules.documents.services.tusd import TusdService
from notepatch.modules.documents.services.upload import UploadService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _check_secret(secret: str | None, header_secret: str | None) -> None:
    expected = get_settings().tusd_webhook_secret
    if not expected or (secret != expected and header_secret != expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook secret")


def _upload_payload(payload: dict) -> dict:
    return payload.get("Event", {}).get("Upload", {}) or {}


@router.post("/tusd")
async def tusd_webhook(
    request: Request,
    secret: str | None = Query(default=None),
    x_tusd_webhook_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
    storage_service: StorageService = Depends(get_storage_service),
) -> dict[str, bool | str | None]:
    _check_secret(secret, x_tusd_webhook_secret)
    payload = await request.json()
    hook_type = payload.get("Type")
    upload = _upload_payload(payload)
    metadata = upload.get("MetaData") or {}
    upload_session_id = metadata.get("upload_session_id")
    document_id = metadata.get("document_id")
    upload_token = metadata.get("upload_token")
    tus_upload_id = upload.get("ID")
    storage = upload.get("Storage") or {}

    if not upload_session_id or not document_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing upload session metadata")

    upload_session = db.scalar(select(UploadSession).where(UploadSession.id == upload_session_id))
    if upload_session is None or upload_session.document_id != document_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

    tusd = TusdService()
    if not tusd.verify_upload_token(
        upload_session.id,
        upload_session.document_id,
        upload_session.object_key,
        upload_token,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid upload token")

    upload_service = UploadService(db, storage_service, tusd)
    tus_upload_url = tusd.build_upload_url(tus_upload_id)
    document = db.scalar(
        select(Document).where(
            Document.workspace_id == upload_session.workspace_id,
            Document.id == upload_session.document_id,
        )
    )
    if upload_session.status == "cancelled" or document is None or document.status == "deleted":
        if upload_session.status != "cancelled":
            upload_session.status = "cancelled"
            db.commit()
        return {"ok": True, "status": "cancelled", "ignored": True}

    if hook_type == "post-create":
        if tus_upload_id:
            upload_service.mark_tusd_created(
                upload_session,
                tus_upload_id=tus_upload_id,
                tus_upload_url=tus_upload_url,
            )
        return {"ok": True, "status": upload_session.status}

    if hook_type == "post-finish":
        if tus_upload_id and not upload_session.tus_upload_id:
            upload_service.mark_tusd_created(
                upload_session,
                tus_upload_id=tus_upload_id,
                tus_upload_url=tus_upload_url,
            )
        local_path = tusd.local_file_path(tus_upload_id or upload_session.tus_upload_id or "", storage.get("Path"))
        document = upload_service.complete_upload(
            upload_session=upload_session,
            tus_upload_id=tus_upload_id,
            tus_upload_url=tus_upload_url,
            local_file_path=local_path,
            file_size=upload.get("Size"),
            mime_type=metadata.get("mime_type"),
        )
        return {"ok": True, "status": document.status}

    if hook_type == "post-terminate":
        upload_service.fail_or_cancel_upload(upload_session, "cancelled")
        return {"ok": True, "status": "cancelled"}

    return {"ok": True, "status": upload_session.status}
