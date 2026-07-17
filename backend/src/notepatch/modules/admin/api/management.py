from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_storage_service
from notepatch.modules.admin.api.admin import _user_read, require_admin_user
from notepatch.modules.admin.models.admin import AdminAuditLog, AdminOperation
from notepatch.modules.admin.schemas.admin import (
    AdminActionResponse,
    AdminAuditLogListResponse,
    AdminHomeworkCreate,
    AdminKnowledgeSearchRequest,
    AdminLearningUnitUpdate,
    AdminOperationListResponse,
    AdminOperationRead,
    AdminPasswordResetResponse,
    AdminProcessRequest,
    AdminUserCreate,
    AdminUserProvisionResponse,
    AdminUserRead,
    AdminUserUpdate,
)
from notepatch.modules.admin.services.operations import AdminOperationsService
from notepatch.modules.ai.models.chat import ChatConversation, ChatMessage
from notepatch.modules.ai.schemas.ai import ChatConversationRead, ChatMessageRead
from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.documents.models.document import Document
from notepatch.modules.documents.services.document import DocumentService
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.learning.models.homework import (
    GradingResult,
    Homework,
    HomeworkReference,
    Mistake,
    Question,
)
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.learning.schemas.homework import (
    GradingConfigUpdate,
    HomeworkRead,
    HomeworkReferenceCreate,
    HomeworkReferenceRead,
    MistakeRead,
    MistakeUpdate,
)
from notepatch.modules.learning.schemas.knowledge import KnowledgeSearchResponse
from notepatch.modules.learning.schemas.learning import (
    KnowledgeChunkRead,
    FlashcardDeckDetail,
    FlashcardDeckRead,
    FlashcardRead,
    LearningUnitRead,
    StudyNoteRevisionCreate,
    StudyNoteRevisionResponse,
    StudyNoteVersionRead,
)
from notepatch.modules.learning.services.homework import HomeworkService
from notepatch.modules.learning.services.knowledge import KnowledgeService
from notepatch.modules.learning.services.notes import StudyNoteService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.database import get_db, utcnow
from notepatch.platform.storage import StorageService


router = APIRouter(prefix="/admin", tags=["admin-management"])


def _page_total(db: Session, query) -> int:
    return int(db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0)


def _workspace(db: Session, workspace_id: str) -> Workspace:
    workspace = db.scalar(select(Workspace).where(Workspace.id == workspace_id, Workspace.type == "personal"))
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


def _target_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.post("/users", response_model=AdminUserProvisionResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: AdminUserCreate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminUserProvisionResponse:
    user, temporary_password = AdminOperationsService(db).create_user(
        admin,
        email=str(payload.email),
        full_name=payload.full_name,
        username=payload.username,
        phone=payload.phone,
    )
    return AdminUserProvisionResponse(user=_user_read(user), temporary_password=temporary_password)


@router.patch("/users/{user_id}", response_model=AdminUserRead)
def update_user(
    user_id: str,
    payload: AdminUserUpdate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminUserRead:
    if not payload.model_fields_set:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="At least one field is required")
    user = AdminOperationsService(db).update_user(
        admin,
        _target_user(db, user_id),
        payload.model_dump(exclude_unset=True),
    )
    return _user_read(user)


@router.post("/users/{user_id}/reset-password", response_model=AdminPasswordResetResponse)
def reset_password(
    user_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminPasswordResetResponse:
    temporary = AdminOperationsService(db).reset_password(admin, _target_user(db, user_id))
    return AdminPasswordResetResponse(user_id=user_id, temporary_password=temporary)


@router.delete("/users/{user_id}", response_model=AdminOperationRead, status_code=status.HTTP_202_ACCEPTED)
def purge_user(
    user_id: str,
    confirm_email: str = Query(min_length=3),
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminOperation:
    target = _target_user(db, user_id)
    if confirm_email.lower().strip() != target.email.lower():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Confirmation email does not match")
    return AdminOperationsService(db).request_user_purge(admin, target)


@router.get("/operations", response_model=AdminOperationListResponse)
def list_operations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    operation_status: str | None = Query(default=None, alias="status"),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminOperationListResponse:
    query = select(AdminOperation)
    if operation_status:
        query = query.where(AdminOperation.status == operation_status)
    total = _page_total(db, query)
    items = db.scalars(
        query.order_by(AdminOperation.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return AdminOperationListResponse(page=page, page_size=page_size, total=total, items=items)


@router.get("/operations/{operation_id}", response_model=AdminOperationRead)
def get_operation(
    operation_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminOperation:
    operation = db.get(AdminOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin operation not found")
    return operation


@router.post("/operations/{operation_id}/retry", response_model=AdminOperationRead, status_code=status.HTTP_202_ACCEPTED)
def retry_operation(
    operation_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminOperation:
    operation = db.get(AdminOperation, operation_id)
    if operation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin operation not found")
    if operation.operation_type != "purge_user" or operation.status not in {"failed", "queued"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Operation cannot be retried")
    actor_workspace = _workspace(db, operation.actor_workspace_id or "")
    task = TaskService(db).create_task(
        workspace_id=actor_workspace.id,
        task_type="purge_user",
        resource_type="admin_operation",
        resource_id=operation.id,
        payload={"admin_operation_id": operation.id, "target_user_id": operation.target_id},
    )
    operation.task_id = task.id
    operation.status = "queued"
    operation.error_message = None
    AdminOperationsService(db).audit(admin, "admin_operation.retry", "admin_operation", operation.id, commit=False)
    db.commit()
    return operation


@router.get("/audit-logs", response_model=AdminAuditLogListResponse)
def list_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    action: str | None = None,
    target_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminAuditLogListResponse:
    query = select(AdminAuditLog)
    if action:
        query = query.where(AdminAuditLog.action == action)
    if target_id:
        query = query.where(AdminAuditLog.target_id == target_id)
    total = _page_total(db, query)
    items = db.scalars(
        query.order_by(AdminAuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return AdminAuditLogListResponse(page=page, page_size=page_size, total=total, items=items)


@router.post("/documents/{document_id}/process", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def process_document(
    document_id: str,
    payload: AdminProcessRequest,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> Task:
    document = db.get(Document, document_id)
    if document is None or document.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if document.status not in {"uploaded", "ready", "failed"} or not storage.object_exists(document.bucket, document.object_key):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document is not ready for processing")
    tasks = TaskService(db)
    active = tasks.find_active_task(
        workspace_id=document.workspace_id,
        task_type="document_processing_pipeline",
        resource_type="document",
        resource_id=document.id,
    )
    if active is not None:
        if payload.force_reprocess:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document processing is already active")
        return active
    task = tasks.create_task(
        workspace_id=document.workspace_id,
        task_type="document_processing_pipeline",
        resource_type="document",
        resource_id=document.id,
        payload={"document_id": document.id, "options": {"force_reprocess": payload.force_reprocess}},
    )
    AdminOperationsService(db).audit(admin, "document.process", "document", document.id, workspace_id=document.workspace_id)
    return task


@router.delete("/documents/{document_id}", response_model=AdminActionResponse, status_code=status.HTTP_202_ACCEPTED)
def delete_document(
    document_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminActionResponse:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    document, task = DocumentService(db, storage).request_delete(document.workspace_id, document.id)
    AdminOperationsService(db).audit(admin, "document.delete", "document", document.id, workspace_id=document.workspace_id)
    return AdminActionResponse(task_id=task.id)


@router.post("/tasks/{task_id}/cancel", response_model=AdminActionResponse)
def cancel_task(
    task_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminActionResponse:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    TaskService(db).request_cancel(task, "Cancelled by administrator")
    AdminOperationsService(db).audit(admin, "task.cancel", "task", task.id, workspace_id=task.workspace_id)
    return AdminActionResponse(task_id=task.id)


@router.post("/tasks/{task_id}/retry", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def retry_task(
    task_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Task:
    original = db.get(Task, task_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if original.status not in {"failed", "cancelled"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only failed or cancelled tasks can be retried")
    task = TaskService(db).create_task(
        workspace_id=original.workspace_id,
        task_type=original.task_type,
        resource_type=original.resource_type,
        resource_id=original.resource_id,
        payload={**(original.payload or {}), "retry_of_task_id": original.id},
    )
    AdminOperationsService(db).audit(admin, "task.retry", "task", original.id, workspace_id=original.workspace_id, after={"new_task_id": task.id})
    return task


@router.get("/learning-units", response_model=list[LearningUnitRead])
def list_learning_units(
    workspace_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[LearningUnit]:
    query = select(LearningUnit)
    if workspace_id:
        query = query.where(LearningUnit.workspace_id == workspace_id)
    return db.scalars(query.order_by(LearningUnit.updated_at.desc()).limit(500)).all()


@router.patch("/learning-units/{learning_unit_id}", response_model=LearningUnitRead)
def update_learning_unit(
    learning_unit_id: str,
    payload: AdminLearningUnitUpdate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> LearningUnit:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    before = {name: getattr(unit, name) for name in ("title", "subject", "grade_level", "topic")}
    for name, value in payload.model_dump(exclude_unset=True).items():
        setattr(unit, name, value)
    AdminOperationsService(db).audit(admin, "learning_unit.update", "learning_unit", unit.id, workspace_id=unit.workspace_id, before=before, after=payload.model_dump(exclude_unset=True), commit=False)
    db.commit()
    db.refresh(unit)
    return unit


@router.delete("/learning-units/{learning_unit_id}", response_model=AdminActionResponse)
def delete_learning_unit(
    learning_unit_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminActionResponse:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    TaskService(db).cancel_active_tasks(
        workspace_id=unit.workspace_id,
        resource_type="learning_unit",
        resource_id=unit.id,
        reason="Learning unit deleted by administrator",
        commit=False,
    )
    for link in db.scalars(select(LearningUnitDocument).where(LearningUnitDocument.learning_unit_id == unit.id)).all():
        document = db.get(Document, link.document_id)
        if document is not None:
            metadata = dict(document.metadata_ or {})
            if metadata.get("learning_unit_id") == unit.id:
                metadata.pop("learning_unit_id", None)
                document.metadata_ = metadata
    storage.delete_prefix(f"workspaces/{unit.workspace_id}/learning-units/{unit.id}/")
    for chunk in db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.workspace_id == unit.workspace_id)).all():
        if (chunk.metadata_ or {}).get("learning_unit_id") == unit.id:
            db.delete(chunk)
    AdminOperationsService(db).audit(admin, "learning_unit.delete", "learning_unit", unit.id, workspace_id=unit.workspace_id, commit=False)
    db.delete(unit)
    db.commit()
    return AdminActionResponse()


@router.get("/learning-units/{learning_unit_id}/notes", response_model=list[StudyNoteVersionRead])
def list_notes(
    learning_unit_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[StudyNoteVersion]:
    return db.scalars(
        select(StudyNoteVersion)
        .where(StudyNoteVersion.learning_unit_id == learning_unit_id)
        .order_by(StudyNoteVersion.version_no.desc())
    ).all()


@router.post(
    "/learning-units/{learning_unit_id}/notes/{base_version_id}/revisions",
    response_model=StudyNoteRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def revise_note(
    learning_unit_id: str,
    base_version_id: str,
    payload: StudyNoteRevisionCreate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> StudyNoteRevisionResponse:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    note, tasks = StudyNoteService(db, storage).create_revision(
        workspace_id=unit.workspace_id,
        learning_unit_id=unit.id,
        base_version_id=base_version_id,
        actor=admin,
        html=payload.html,
        title=payload.title,
        edit_summary=payload.edit_summary,
        edit_origin="admin",
    )
    AdminOperationsService(db).audit(admin, "study_note.revise", "study_note", note.id, workspace_id=unit.workspace_id, after={"source_version_id": base_version_id})
    return StudyNoteRevisionResponse(
        note=StudyNoteVersionRead.model_validate(note),
        downstream_tasks=[{"id": item.id, "task_type": item.task_type, "status": item.status} for item in tasks],
    )


@router.post("/learning-units/{learning_unit_id}/notes/regenerate", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def regenerate_note(
    learning_unit_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Task:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    task = TaskService(db).create_task(
        workspace_id=unit.workspace_id,
        task_type="generate_study_notes",
        resource_type="learning_unit",
        resource_id=unit.id,
        payload={
            "learning_unit_id": unit.id,
            "expected_knowledge_revision": unit.knowledge_revision,
            "force_reprocess": True,
            "reason": "admin_regenerate",
        },
    )
    AdminOperationsService(db).audit(admin, "study_note.regenerate", "learning_unit", unit.id, workspace_id=unit.workspace_id)
    return task


@router.post("/learning-units/{learning_unit_id}/flashcards", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def generate_flashcards(
    learning_unit_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Task:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    note = db.scalar(
        select(StudyNoteVersion)
        .where(StudyNoteVersion.learning_unit_id == unit.id)
        .order_by(StudyNoteVersion.version_no.desc())
    )
    task = TaskService(db).create_task(
        workspace_id=unit.workspace_id,
        task_type="generate_flashcards",
        resource_type="learning_unit",
        resource_id=unit.id,
        payload={
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id if note else None,
            "expected_attempt_revision": unit.attempt_revision,
            "reason": "admin_generate",
        },
    )
    AdminOperationsService(db).audit(admin, "flashcards.generate", "learning_unit", unit.id, workspace_id=unit.workspace_id)
    return task


@router.get("/learning-units/{learning_unit_id}/flashcard-decks", response_model=list[FlashcardDeckRead])
def list_admin_flashcard_decks(
    learning_unit_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[FlashcardDeck]:
    if db.get(LearningUnit, learning_unit_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    return db.scalars(
        select(FlashcardDeck)
        .where(FlashcardDeck.learning_unit_id == learning_unit_id)
        .order_by(FlashcardDeck.version_no.desc())
    ).all()


@router.get("/flashcard-decks/{deck_id}", response_model=FlashcardDeckDetail)
def get_admin_flashcard_deck(
    deck_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> FlashcardDeckDetail:
    deck = db.get(FlashcardDeck, deck_id)
    if deck is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flashcard deck not found")
    cards = db.scalars(select(Flashcard).where(Flashcard.deck_id == deck.id).order_by(Flashcard.rank.asc())).all()
    return FlashcardDeckDetail(
        deck=FlashcardDeckRead.model_validate(deck),
        cards=[FlashcardRead.model_validate(card) for card in cards],
    )


@router.post("/learning-units/{learning_unit_id}/highlight", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def highlight_latest_note(
    learning_unit_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Task:
    unit = db.get(LearningUnit, learning_unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    note = db.scalar(
        select(StudyNoteVersion)
        .where(StudyNoteVersion.learning_unit_id == unit.id)
        .order_by(StudyNoteVersion.version_no.desc())
    )
    mistakes = [
        item
        for item in db.scalars(
            select(Mistake).where(Mistake.workspace_id == unit.workspace_id, Mistake.status == "open")
        ).all()
        if (item.metadata_ or {}).get("learning_unit_id") == unit.id
    ]
    if note is None or not mistakes:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A study note and open mistakes are required")
    task = TaskService(db).create_task(
        workspace_id=unit.workspace_id,
        task_type="highlight_study_notes",
        resource_type="learning_unit",
        resource_id=unit.id,
        payload={
            "learning_unit_id": unit.id,
            "mistake_ids": [item.id for item in mistakes],
            "expected_note_version_id": note.id,
            "reason": "admin_highlight",
        },
    )
    AdminOperationsService(db).audit(admin, "study_note.highlight", "study_note", note.id, workspace_id=unit.workspace_id)
    return task


@router.delete("/notes/{note_version_id}", response_model=AdminActionResponse)
def delete_note(
    note_version_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminActionResponse:
    note = db.get(StudyNoteVersion, note_version_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note not found")
    for key in (note.html_object_key, note.json_object_key, note.highlighted_html_object_key, note.highlight_map_object_key):
        if key:
            storage.delete_object(storage.bucket, key)
    AdminOperationsService(db).audit(admin, "study_note.delete", "study_note", note.id, workspace_id=note.workspace_id, commit=False)
    db.delete(note)
    db.commit()
    return AdminActionResponse()


@router.get("/notes/{note_version_id}/content")
def get_note_content(
    note_version_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> dict:
    note = db.get(StudyNoteVersion, note_version_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note not found")
    return {
        "id": note.id,
        "title": note.title,
        "html": storage.get_text_artifact(note.html_object_key, bucket=storage.bucket),
        "version_no": note.version_no,
    }


@router.get("/notes/{note_version_id}/download-url")
def get_note_download_url(
    note_version_id: str,
    kind: str = Query(default="html", pattern="^(html|json|highlighted_html|highlight_map)$"),
    expires_seconds: int = Query(default=900, ge=60, le=86400),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> dict:
    note = db.get(StudyNoteVersion, note_version_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note not found")
    key = {
        "html": note.html_object_key,
        "json": note.json_object_key,
        "highlighted_html": note.highlighted_html_object_key,
        "highlight_map": note.highlight_map_object_key,
    }[kind]
    if not key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study note artifact not found")
    return {
        "id": note.id,
        "resource_type": "study_note",
        "filename": storage.filename_for_object_key(key),
        "mime_type": "application/json" if kind in {"json", "highlight_map"} else "text/html",
        "expires_in": expires_seconds,
        "download_url": storage.create_presigned_download_url(storage.bucket, key, expires_seconds),
    }


@router.get("/knowledge-chunks", response_model=list[KnowledgeChunkRead])
def list_knowledge_chunks(
    workspace_id: str | None = None,
    learning_unit_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[KnowledgeChunk]:
    query = select(KnowledgeChunk)
    if workspace_id:
        query = query.where(KnowledgeChunk.workspace_id == workspace_id)
    chunks = db.scalars(query.order_by(KnowledgeChunk.created_at.desc()).limit(500)).all()
    if learning_unit_id:
        chunks = [item for item in chunks if (item.metadata_ or {}).get("learning_unit_id") == learning_unit_id]
    return chunks


@router.post("/knowledge/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    payload: AdminKnowledgeSearchRequest,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> KnowledgeSearchResponse:
    _workspace(db, payload.workspace_id)
    return KnowledgeSearchResponse(
        items=KnowledgeService(db).search(
            workspace_id=payload.workspace_id,
            query=payload.query,
            learning_unit_id=payload.learning_unit_id,
            subject=payload.subject,
            limit=payload.limit,
            owner=f"admin-knowledge-search:{admin.id}",
        )
    )


@router.delete("/knowledge-chunks/{chunk_id}", response_model=AdminActionResponse)
def delete_knowledge_chunk(
    chunk_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> AdminActionResponse:
    chunk = db.get(KnowledgeChunk, chunk_id)
    if chunk is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge chunk not found")
    AdminOperationsService(db).audit(admin, "knowledge_chunk.delete", "knowledge_chunk", chunk.id, workspace_id=chunk.workspace_id, commit=False)
    db.delete(chunk)
    db.commit()
    return AdminActionResponse()


@router.get("/homeworks", response_model=list[HomeworkRead])
def list_homeworks(
    workspace_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[Homework]:
    query = select(Homework)
    if workspace_id:
        query = query.where(Homework.workspace_id == workspace_id)
    return db.scalars(query.order_by(Homework.created_at.desc()).limit(500)).all()


@router.post("/homeworks", response_model=HomeworkRead, status_code=status.HTTP_201_CREATED)
def create_homework(
    payload: AdminHomeworkCreate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Homework:
    workspace = _workspace(db, payload.workspace_id)
    owner = _target_user(db, workspace.owner_user_id)
    homework = HomeworkService(db).create_homework(
        workspace_id=workspace.id,
        user=owner,
        title=payload.title,
        description=payload.description,
        document_id=payload.document_id,
        due_at=None,
        rubric_text=payload.rubric_text,
        max_score=payload.max_score,
        metadata={"created_by_admin_id": admin.id},
    )
    AdminOperationsService(db).audit(admin, "homework.create", "homework", homework.id, workspace_id=workspace.id)
    return homework


@router.get("/homeworks/{homework_id}", response_model=HomeworkRead)
def get_homework(
    homework_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Homework:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    return homework


@router.patch("/homeworks/{homework_id}/grading-config", response_model=HomeworkRead)
def update_homework_config(
    homework_id: str,
    payload: GradingConfigUpdate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Homework:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    updated = HomeworkService(db).update_grading_config(
        homework.workspace_id,
        homework.id,
        rubric_text=payload.rubric_text,
        max_score=payload.max_score,
        fields_set=set(payload.model_fields_set),
    )
    AdminOperationsService(db).audit(admin, "homework.update_grading_config", "homework", homework.id, workspace_id=homework.workspace_id)
    return updated


@router.get("/homeworks/{homework_id}/references", response_model=list[HomeworkReferenceRead])
def list_references(
    homework_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[HomeworkReference]:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    return HomeworkService(db).list_references(homework.workspace_id, homework.id)


@router.post("/homeworks/{homework_id}/references", response_model=HomeworkReferenceRead, status_code=status.HTTP_201_CREATED)
def add_reference(
    homework_id: str,
    payload: HomeworkReferenceCreate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> HomeworkReference:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    reference = HomeworkService(db).add_reference(
        homework.workspace_id,
        homework.id,
        document_id=payload.document_id,
        reference_type=payload.reference_type,
    )
    AdminOperationsService(db).audit(admin, "homework_reference.create", "homework_reference", reference.id, workspace_id=homework.workspace_id)
    return reference


@router.delete("/homeworks/{homework_id}/references/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_reference(
    homework_id: str,
    reference_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> None:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    HomeworkService(db).delete_reference(homework.workspace_id, homework.id, reference_id)
    AdminOperationsService(db).audit(admin, "homework_reference.delete", "homework_reference", reference_id, workspace_id=homework.workspace_id)


@router.post("/homeworks/{homework_id}/grade", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def grade_homework(
    homework_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Task:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    HomeworkService(db).validate_grading_inputs(homework)
    workspace = _workspace(db, homework.workspace_id)
    task = TaskService(db).create_task(
        workspace_id=homework.workspace_id,
        task_type="grade_homework",
        resource_type="homework",
        resource_id=homework.id,
        payload={"homework_id": homework.id, "student_user_id": workspace.owner_user_id, "reason": "admin_grade"},
    )
    AdminOperationsService(db).audit(admin, "homework.grade", "homework", homework.id, workspace_id=homework.workspace_id)
    return task


@router.delete("/homeworks/{homework_id}", response_model=AdminActionResponse)
def delete_homework(
    homework_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
    storage: StorageService = Depends(get_storage_service),
) -> AdminActionResponse:
    homework = db.get(Homework, homework_id)
    if homework is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
    TaskService(db).cancel_active_tasks(workspace_id=homework.workspace_id, resource_type="homework", resource_id=homework.id, reason="Homework deleted by administrator", commit=False)
    gradings = db.scalars(select(GradingResult).where(GradingResult.homework_id == homework.id)).all()
    grading_ids = [item.id for item in gradings]
    for mistake in db.scalars(select(Mistake).where(Mistake.grading_result_id.in_(grading_ids))).all() if grading_ids else []:
        db.delete(mistake)
    for grading in gradings:
        if grading.report_storage_key:
            storage.delete_object(storage.bucket, grading.report_storage_key)
        db.delete(grading)
    for question in db.scalars(select(Question).where(Question.homework_id == homework.id)).all():
        question.homework_id = None
    AdminOperationsService(db).audit(admin, "homework.delete", "homework", homework.id, workspace_id=homework.workspace_id, commit=False)
    db.delete(homework)
    db.commit()
    return AdminActionResponse()


@router.get("/mistakes", response_model=list[MistakeRead])
def list_mistakes(
    workspace_id: str | None = None,
    mistake_status: str | None = Query(default=None, alias="status"),
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[Mistake]:
    query = select(Mistake)
    if workspace_id:
        query = query.where(Mistake.workspace_id == workspace_id)
    if mistake_status:
        query = query.where(Mistake.status == mistake_status)
    return db.scalars(query.order_by(Mistake.created_at.desc()).limit(500)).all()


@router.patch("/mistakes/{mistake_id}", response_model=MistakeRead)
def update_mistake(
    mistake_id: str,
    payload: MistakeUpdate,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> Mistake:
    mistake = db.get(Mistake, mistake_id)
    if mistake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mistake not found")
    before = {"status": mistake.status, "description": mistake.description}
    if payload.status is not None:
        mistake.status = payload.status
    if payload.description is not None:
        mistake.description = payload.description
    if payload.metadata is not None:
        mistake.metadata_ = {**(mistake.metadata_ or {}), **payload.metadata}
    AdminOperationsService(db).audit(admin, "mistake.update", "mistake", mistake.id, workspace_id=mistake.workspace_id, before=before, after={"status": mistake.status, "description": mistake.description}, commit=False)
    db.commit()
    db.refresh(mistake)
    return mistake


@router.get("/conversations", response_model=list[ChatConversationRead])
def list_conversations(
    workspace_id: str | None = None,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[ChatConversation]:
    query = select(ChatConversation).where(ChatConversation.deleted_at.is_(None))
    if workspace_id:
        query = query.where(ChatConversation.workspace_id == workspace_id)
    return db.scalars(query.order_by(ChatConversation.updated_at.desc()).limit(500)).all()


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatMessageRead])
def list_messages(
    conversation_id: str,
    _admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> list[ChatMessage]:
    conversation = db.get(ChatConversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return db.scalars(
        select(ChatMessage).where(ChatMessage.conversation_id == conversation.id).order_by(ChatMessage.created_at.asc())
    ).all()


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: str,
    admin: User = Depends(require_admin_user),
    db: Session = Depends(get_db),
) -> None:
    conversation = db.get(ChatConversation, conversation_id)
    if conversation is None or conversation.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    ChatService(db).delete_conversation(conversation)
    AdminOperationsService(db).audit(admin, "conversation.delete", "conversation", conversation.id, workspace_id=conversation.workspace_id)
