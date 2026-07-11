from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_task_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import require_member_permission
from notepatch.modules.documents.models.document import Document
from notepatch.modules.ai.models.chat import ChatConversation
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.ai.schemas.ai import (
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
from notepatch.modules.tasks.services.task import TaskService

router = APIRouter(prefix="/workspaces/{workspace_id}/ai", tags=["ai"])


@router.post("/chat", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def chat(
    workspace_id: str,
    payload: ChatRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    require_member_permission(task_service.db, member, "ai.run")
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
    return task_service.create_task(
        workspace_id=workspace_id,
        task_type="generate_flashcards",
        resource_type="document" if payload.document_id else None,
        resource_id=payload.document_id,
        payload={"document_id": payload.document_id, "subject": payload.subject, "options": payload.options},
    )
