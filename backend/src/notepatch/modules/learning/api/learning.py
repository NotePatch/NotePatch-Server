from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.learning import (
    KnowledgeChunkRead,
    LearningUnitDetailResponse,
    LearningUnitDocumentRead,
    LearningUnitRead,
    StudyNoteDownloadUrlResponse,
    StudyNoteVersionRead,
)
from notepatch.platform.storage import StorageService

router = APIRouter(prefix="/workspaces/{workspace_id}/learning-units", tags=["learning"])


def _get_learning_unit(db: Session, workspace_id: str, learning_unit_id: str) -> LearningUnit:
    learning_unit = db.scalar(
        select(LearningUnit).where(LearningUnit.workspace_id == workspace_id, LearningUnit.id == learning_unit_id)
    )
    if learning_unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    return learning_unit


def _documents_for_unit(db: Session, workspace_id: str, learning_unit_id: str) -> list[LearningUnitDocumentRead]:
    rows = db.execute(
        select(LearningUnitDocument, Document)
        .join(Document, Document.id == LearningUnitDocument.document_id)
        .where(
            LearningUnitDocument.workspace_id == workspace_id,
            LearningUnitDocument.learning_unit_id == learning_unit_id,
            Document.workspace_id == workspace_id,
            Document.status != "deleted",
        )
        .order_by(LearningUnitDocument.created_at.asc())
    ).all()
    return [
        LearningUnitDocumentRead(
            id=link.id,
            document_id=document.id,
            role=link.role,
            title=document.title,
            original_filename=document.original_filename,
            document_kind=document.document_kind,
            file_type=document.file_type,
            status=document.status,
            created_at=link.created_at,
        )
        for link, document in rows
    ]


def _note_download_urls(storage: StorageService, note: StudyNoteVersion, expires_seconds: int) -> dict[str, str]:
    keys = {
        "markdown": note.markdown_object_key,
        "json": note.json_object_key,
        "highlighted": note.highlighted_object_key,
        "highlight_map": note.highlight_map_object_key,
    }
    return {
        kind: storage.create_presigned_download_url(storage.bucket, object_key, expires_seconds)
        for kind, object_key in keys.items()
        if object_key
    }


@router.get("", response_model=list[LearningUnitRead])
def list_learning_units(
    workspace_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    subject: str | None = None,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[LearningUnit]:
    query = select(LearningUnit).where(LearningUnit.workspace_id == workspace_id)
    if subject:
        query = query.where(LearningUnit.subject == subject)
    return db.scalars(
        query.order_by(LearningUnit.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()


@router.get("/{learning_unit_id}", response_model=LearningUnitDetailResponse)
def get_learning_unit(
    workspace_id: str,
    learning_unit_id: str,
    include_download_url: bool = Query(default=False),
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> LearningUnitDetailResponse:
    learning_unit = _get_learning_unit(db, workspace_id, learning_unit_id)
    latest_note = db.scalar(
        select(StudyNoteVersion)
        .where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == learning_unit.id,
        )
        .order_by(StudyNoteVersion.version_no.desc())
    )
    note_read = StudyNoteVersionRead.model_validate(latest_note) if latest_note is not None else None
    if note_read is not None and include_download_url:
        note_read.download_urls = _note_download_urls(storage, latest_note, expires_seconds)
    return LearningUnitDetailResponse(
        learning_unit=LearningUnitRead.model_validate(learning_unit),
        documents=_documents_for_unit(db, workspace_id, learning_unit.id),
        latest_note=note_read,
    )


@router.get("/{learning_unit_id}/knowledge-chunks", response_model=list[KnowledgeChunkRead])
def list_learning_unit_knowledge_chunks(
    workspace_id: str,
    learning_unit_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[KnowledgeChunk]:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    chunks = db.scalars(
        select(KnowledgeChunk)
        .outerjoin(Document, Document.id == KnowledgeChunk.document_id)
        .where(KnowledgeChunk.workspace_id == workspace_id)
        .where((KnowledgeChunk.document_id.is_(None)) | (Document.status != "deleted"))
        .order_by(KnowledgeChunk.created_at.desc())
        .limit(500)
    ).all()
    return [chunk for chunk in chunks if (chunk.metadata_ or {}).get("learning_unit_id") == learning_unit_id]


@router.get("/{learning_unit_id}/notes", response_model=list[StudyNoteVersionRead])
def list_study_notes(
    workspace_id: str,
    learning_unit_id: str,
    include_download_url: bool = Query(default=False),
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> list[StudyNoteVersionRead]:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    notes = db.scalars(
        select(StudyNoteVersion)
        .where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == learning_unit_id,
        )
        .order_by(StudyNoteVersion.version_no.desc())
    ).all()
    result = [StudyNoteVersionRead.model_validate(note) for note in notes]
    if include_download_url:
        for item, note in zip(result, notes, strict=True):
            item.download_urls = _note_download_urls(storage, note, expires_seconds)
    return result


@router.get("/{learning_unit_id}/notes/{note_version_id}/download-url", response_model=StudyNoteDownloadUrlResponse)
def get_study_note_download_url(
    workspace_id: str,
    learning_unit_id: str,
    note_version_id: str,
    kind: Literal["markdown", "json", "highlighted", "highlight_map"] = Query(default="markdown"),
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> StudyNoteDownloadUrlResponse:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    note = db.scalar(
        select(StudyNoteVersion).where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == learning_unit_id,
            StudyNoteVersion.id == note_version_id,
        )
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note not found")
    object_key_by_kind = {
        "markdown": note.markdown_object_key,
        "json": note.json_object_key,
        "highlighted": note.highlighted_object_key,
        "highlight_map": note.highlight_map_object_key,
    }
    object_key = object_key_by_kind[kind]
    if not object_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note artifact not found")
    return StudyNoteDownloadUrlResponse(
        note_version_id=note.id,
        learning_unit_id=learning_unit_id,
        kind=kind,
        filename=storage.filename_for_object_key(object_key),
        expires_in=expires_seconds,
        download_url=storage.create_presigned_download_url(storage.bucket, object_key, expires_seconds),
    )
