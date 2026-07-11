from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import require_member_permission
from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.documents.schemas.document import ArtifactCreate, ArtifactDownloadUrlResponse, DocumentArtifactRead
from notepatch.modules.documents.services.document import DocumentService
from notepatch.platform.storage import StorageService

router = APIRouter(prefix="/workspaces/{workspace_id}/documents/{document_id}/artifacts", tags=["artifacts"])


def _validate_artifact_object_key(workspace_id: str, document_id: str, object_key: str) -> None:
    allowed_prefix = f"workspaces/{workspace_id}/documents/{document_id}/artifacts/"
    if not object_key.startswith(allowed_prefix):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="artifact object_key must be under the document artifacts prefix",
        )


def _get_artifact(db: Session, workspace_id: str, document_id: str, artifact_id: str) -> DocumentArtifact:
    artifact = db.scalar(
        select(DocumentArtifact).where(
            DocumentArtifact.workspace_id == workspace_id,
            DocumentArtifact.document_id == document_id,
            DocumentArtifact.id == artifact_id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return artifact


@router.post("", response_model=DocumentArtifactRead, status_code=status.HTTP_201_CREATED)
def create_artifact(
    workspace_id: str,
    document_id: str,
    payload: ArtifactCreate,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> DocumentArtifact:
    require_member_permission(db, member, "documents.write")
    DocumentService(db, storage).get_document(workspace_id, document_id)
    _validate_artifact_object_key(workspace_id, document_id, payload.object_key)
    bucket = payload.bucket or storage.bucket
    if bucket != storage.bucket:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Artifact bucket must use the configured storage bucket")
    if not storage.object_exists(bucket, payload.object_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Artifact object does not exist")
    object_metadata = storage.get_object_metadata(bucket, payload.object_key)
    artifact = DocumentArtifact(
        workspace_id=workspace_id,
        document_id=document_id,
        artifact_type=payload.artifact_type,
        bucket=bucket,
        object_key=payload.object_key,
        mime_type=payload.mime_type or object_metadata.get("content_type"),
        file_size=object_metadata.get("content_length"),
        metadata_=payload.metadata,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact


@router.get("", response_model=list[DocumentArtifactRead])
def list_artifacts(
    workspace_id: str,
    document_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> list[DocumentArtifact]:
    DocumentService(db, storage).get_document(workspace_id, document_id)
    return db.scalars(
        select(DocumentArtifact)
        .where(DocumentArtifact.workspace_id == workspace_id, DocumentArtifact.document_id == document_id)
        .order_by(DocumentArtifact.created_at.asc())
    ).all()


@router.get("/{artifact_id}/download-url", response_model=ArtifactDownloadUrlResponse)
def get_artifact_download_url(
    workspace_id: str,
    document_id: str,
    artifact_id: str,
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> ArtifactDownloadUrlResponse:
    DocumentService(db, storage).get_document(workspace_id, document_id)
    artifact = _get_artifact(db, workspace_id, document_id, artifact_id)
    return ArtifactDownloadUrlResponse(
        artifact_id=artifact.id,
        document_id=artifact.document_id,
        artifact_type=artifact.artifact_type,
        filename=storage.filename_for_object_key(artifact.object_key),
        mime_type=artifact.mime_type,
        expires_in=expires_seconds,
        download_url=storage.create_presigned_artifact_download_url(
            artifact.bucket,
            artifact.object_key,
            expires_seconds,
        ),
    )
