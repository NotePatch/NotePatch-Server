from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service, get_task_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.learning import (
    KnowledgeChunkRead,
    FlashcardDeckDetail,
    FlashcardDeckRead,
    FlashcardRead,
    LearningUnitDetailResponse,
    LearningUnitDocumentRead,
    LearningUnitRead,
    StudyNoteDownloadUrlResponse,
    StudyNoteVersionRead,
    StudyNoteRevisionCreate,
    StudyNoteRevisionResponse,
    LearningUnitMergeRequest,
)
from notepatch.modules.identity.models.user import User
from notepatch.entrypoints.deps import get_current_user
from notepatch.modules.learning.services.notes import StudyNoteService
from notepatch.modules.learning.services.note_render import NoteRenderService
from notepatch.platform.storage import StorageService
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.tasks.services.task import TaskService

router = APIRouter(prefix="/workspaces/{workspace_id}/learning-units", tags=["learning"])


def _get_learning_unit(db: Session, workspace_id: str, learning_unit_id: str) -> LearningUnit:
    learning_unit = db.scalar(
        select(LearningUnit).where(LearningUnit.workspace_id == workspace_id, LearningUnit.id == learning_unit_id, LearningUnit.merged_into_id.is_(None))
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
        "html": note.html_object_key,
        "json": note.json_object_key,
        "highlighted_html": note.highlighted_html_object_key,
        "highlight_map": note.highlight_map_object_key,
    }
    urls = {
        kind: storage.create_presigned_download_url(storage.bucket, object_key, expires_seconds)
        for kind, object_key in keys.items()
        if object_key
    }
    urls["rendered_html"] = NoteRenderService().create_url(note, expires_seconds)
    return urls


@router.get("", response_model=list[LearningUnitRead])
def list_learning_units(
    workspace_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    subject: str | None = None,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[LearningUnit]:
    query = select(LearningUnit).where(
        LearningUnit.workspace_id == workspace_id,
        LearningUnit.merged_into_id.is_(None),
    )
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
    kind: Literal["html", "json", "highlighted_html", "highlight_map", "rendered_html"] = Query(default="html"),
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
        "html": note.html_object_key,
        "json": note.json_object_key,
        "highlighted_html": note.highlighted_html_object_key,
        "highlight_map": note.highlight_map_object_key,
    }
    if kind == "rendered_html":
        return StudyNoteDownloadUrlResponse(
            note_version_id=note.id,
            learning_unit_id=learning_unit_id,
            kind=kind,
            filename="study-note.html",
            expires_in=expires_seconds,
            download_url=NoteRenderService().create_url(note, expires_seconds),
        )
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


@router.post(
    "/{learning_unit_id}/notes/{base_version_id}/revisions",
    response_model=StudyNoteRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_study_note_revision(
    workspace_id: str,
    learning_unit_id: str,
    base_version_id: str,
    payload: StudyNoteRevisionCreate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> StudyNoteRevisionResponse:
    note, downstream = StudyNoteService(db, storage).create_revision(
        workspace_id=workspace_id,
        learning_unit_id=learning_unit_id,
        base_version_id=base_version_id,
        actor=current_user,
        html=payload.html,
        title=payload.title,
        edit_summary=payload.edit_summary,
    )
    return StudyNoteRevisionResponse(
        note=StudyNoteVersionRead.model_validate(note),
        downstream_tasks=[{"id": task.id, "task_type": task.task_type, "status": task.status} for task in downstream],
    )


def _deck_detail(db: Session, deck: FlashcardDeck) -> FlashcardDeckDetail:
    cards = db.scalars(
        select(Flashcard)
        .where(Flashcard.workspace_id == deck.workspace_id, Flashcard.deck_id == deck.id)
        .order_by(Flashcard.rank.asc())
    ).all()
    return FlashcardDeckDetail(
        deck=FlashcardDeckRead.model_validate(deck),
        cards=[FlashcardRead.model_validate(card) for card in cards],
    )


@router.get("/{learning_unit_id}/flashcard-decks", response_model=list[FlashcardDeckRead])
def list_flashcard_decks(
    workspace_id: str,
    learning_unit_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[FlashcardDeck]:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    return db.scalars(
        select(FlashcardDeck)
        .where(
            FlashcardDeck.workspace_id == workspace_id,
            FlashcardDeck.learning_unit_id == learning_unit_id,
        )
        .order_by(FlashcardDeck.version_no.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()


@router.get("/{learning_unit_id}/flashcard-decks/latest", response_model=FlashcardDeckDetail)
def get_latest_flashcard_deck(
    workspace_id: str,
    learning_unit_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> FlashcardDeckDetail:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    deck = db.scalar(
        select(FlashcardDeck)
        .where(
            FlashcardDeck.workspace_id == workspace_id,
            FlashcardDeck.learning_unit_id == learning_unit_id,
        )
        .order_by(FlashcardDeck.version_no.desc())
    )
    if deck is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flashcard deck not found")
    return _deck_detail(db, deck)


@router.get("/{learning_unit_id}/flashcard-decks/{deck_id}", response_model=FlashcardDeckDetail)
def get_flashcard_deck(
    workspace_id: str,
    learning_unit_id: str,
    deck_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> FlashcardDeckDetail:
    _get_learning_unit(db, workspace_id, learning_unit_id)
    deck = db.scalar(
        select(FlashcardDeck).where(
            FlashcardDeck.workspace_id == workspace_id,
            FlashcardDeck.learning_unit_id == learning_unit_id,
            FlashcardDeck.id == deck_id,
        )
    )
    if deck is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flashcard deck not found")
    return _deck_detail(db, deck)


@router.post("/{target_learning_unit_id}/merge", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
def merge_learning_units(
    workspace_id: str,
    target_learning_unit_id: str,
    payload: LearningUnitMergeRequest,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    task_service: TaskService = Depends(get_task_service),
):
    source_ids = list(dict.fromkeys(payload.source_learning_unit_ids))
    if target_learning_unit_id in source_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Target cannot be a source")
    units = db.scalars(
        select(LearningUnit).where(
            LearningUnit.workspace_id == workspace_id,
            LearningUnit.id.in_([target_learning_unit_id, *source_ids]),
            LearningUnit.merged_into_id.is_(None),
        )
    ).all()
    if len(units) != len(source_ids) + 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    active = task_service.find_active_task(
        workspace_id=workspace_id,
        task_type="merge_learning_units",
        resource_type="learning_unit",
        resource_id=target_learning_unit_id,
    )
    if active is not None:
        return active
    return task_service.create_task(
        workspace_id=workspace_id,
        task_type="merge_learning_units",
        resource_type="learning_unit",
        resource_id=target_learning_unit_id,
        payload={
            "target_learning_unit_id": target_learning_unit_id,
            "source_learning_unit_ids": source_ids,
        },
    )
