from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import (
    get_ai_model_catalog_service,
    get_current_user,
    get_task_service,
    get_workspace_member,
)
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import require_member_permission
from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.ai.models.chat import ChatConversation
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.ai.schemas.ai import (
    AiModelCatalogRead,
    AiModelSelectionRead,
    AiModelSelectionUpdate,
    ChatConversationPage,
    ChatConversationRead,
    ChatConversationUpdate,
    ChatMessagePage,
    ChatMessageRead,
    ChatRequest,
    GenerateFlashcardsRequest,
)
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.ai.services.chat import ChatConversationNotFoundError, ChatService
from notepatch.modules.ai.services.model_catalog import (
    AiModelCatalogService,
    AiModelCatalogUnavailableError,
    AiModelNotFoundError,
)
from notepatch.modules.ai.services.model_selection import AiModelSelectionService
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.rate_limit import RateLimiter

router = APIRouter(prefix="/workspaces/{workspace_id}/ai", tags=["ai"])


@router.get("/models", response_model=AiModelCatalogRead)
def list_ai_models(
    workspace_id: str,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    catalog: AiModelCatalogService = Depends(get_ai_model_catalog_service),
) -> dict:
    require_member_permission(db, member, "ai.run")
    try:
        result = catalog.get_catalog()
        selected = AiModelSelectionService(db).selected_for_user(current_user)
    except AiModelCatalogUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {**result, "selected_model": selected}


@router.put("/model", response_model=AiModelSelectionRead)
def select_ai_model(
    workspace_id: str,
    payload: AiModelSelectionUpdate,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    catalog: AiModelCatalogService = Depends(get_ai_model_catalog_service),
) -> AiModelSelectionRead:
    require_member_permission(db, member, "ai.run")
    preferred_model = None
    if payload.model_id is not None:
        try:
            preferred_model = catalog.validate_model(payload.model_id)
        except AiModelNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except AiModelCatalogUnavailableError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    current_user.preferred_ai_model = preferred_model
    db.commit()
    db.refresh(current_user)
    return AiModelSelectionRead(
        selected_model=preferred_model or catalog.default_model,
        preferred_model=preferred_model,
        default_model=catalog.default_model,
    )


@router.post("/chat", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def chat(
    workspace_id: str,
    payload: ChatRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    require_member_permission(task_service.db, member, "ai.run")
    RateLimiter().check("ai", current_user.id, get_settings().ai_rate_limit_per_minute)
    try:
        return ChatService(task_service.db).create_chat_task(
            workspace_id=workspace_id,
            user=current_user,
            prompt=payload.prompt,
            input_payload=payload.input,
            options=payload.options,
            conversation_id=payload.conversation_id,
            task_service=task_service,
        )
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found") from exc


@router.get("/conversations", response_model=ChatConversationPage)
def list_conversations(
    workspace_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatConversationPage:
    items, total = ChatService(db).list_conversations(
        workspace_id=workspace_id,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    return ChatConversationPage(items=items, page=page, page_size=page_size, total=total)


@router.get("/conversations/{conversation_id}", response_model=ChatConversationRead)
def get_conversation(
    workspace_id: str,
    conversation_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatConversation:
    try:
        return ChatService(db).get_active_conversation(
            workspace_id=workspace_id,
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found") from exc


@router.get("/conversations/{conversation_id}/messages", response_model=ChatMessagePage)
def list_messages(
    workspace_id: str,
    conversation_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatMessagePage:
    service = ChatService(db)
    try:
        conversation = service.get_active_conversation(
            workspace_id=workspace_id,
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found") from exc
    items, total = service.list_messages(
        conversation=conversation,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
    )
    return ChatMessagePage(items=items, page=page, page_size=page_size, total=total)


@router.patch("/conversations/{conversation_id}", response_model=ChatConversationRead)
def rename_conversation(
    workspace_id: str,
    conversation_id: str,
    payload: ChatConversationUpdate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatConversation:
    service = ChatService(db)
    try:
        conversation = service.get_active_conversation(
            workspace_id=workspace_id,
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found") from exc
    return service.rename_conversation(conversation, payload.title)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    workspace_id: str,
    conversation_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    service = ChatService(db)
    try:
        conversation = service.get_active_conversation(
            workspace_id=workspace_id,
            user_id=current_user.id,
            conversation_id=conversation_id,
        )
    except ChatConversationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found") from exc
    service.delete_conversation(conversation)


@router.post("/generate-flashcards", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def generate_flashcards(
    workspace_id: str,
    payload: GenerateFlashcardsRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    require_member_permission(db, member, "ai.run")
    unit: LearningUnit | None = None
    if payload.learning_unit_id is not None:
        unit = db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == workspace_id,
                LearningUnit.id == payload.learning_unit_id,
                LearningUnit.merged_into_id.is_(None),
            )
        )
        if unit is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
    if payload.document_id is not None:
        document = db.scalar(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.id == payload.document_id,
                Document.status != "deleted",
            )
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        link = db.scalar(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == workspace_id,
                LearningUnitDocument.document_id == document.id,
            )
        )
        if link is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document has no learning unit")
        document_unit = db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == workspace_id,
                LearningUnit.id == link.learning_unit_id,
                LearningUnit.merged_into_id.is_(None),
            )
        )
        if document_unit is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Document learning unit is unavailable")
        if unit is not None and unit.id != document_unit.id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="document_id does not belong to learning_unit_id",
            )
        unit = document_unit
    if unit is None and payload.subject is not None:
        matches = db.scalars(
            select(LearningUnit).where(
                LearningUnit.workspace_id == workspace_id,
                LearningUnit.subject == payload.subject,
                LearningUnit.merged_into_id.is_(None),
            )
        ).all()
        if len(matches) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Subject matches multiple learning units; provide learning_unit_id",
            )
        unit = matches[0] if matches else None
    if unit is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="learning_unit_id or a linked document_id is required",
        )
    note = db.scalar(
        select(StudyNoteVersion)
        .where(
            StudyNoteVersion.workspace_id == workspace_id,
            StudyNoteVersion.learning_unit_id == unit.id,
        )
        .order_by(StudyNoteVersion.version_no.desc())
    )
    if note is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Learning unit has no study note")
    return task_service.create_task(
        workspace_id=workspace_id,
        task_type="generate_flashcards",
        resource_type="learning_unit",
        resource_id=unit.id,
        payload={
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id,
            "expected_attempt_revision": unit.attempt_revision,
            "reason": "manual_ai_endpoint",
            "options": payload.options,
        },
    )
