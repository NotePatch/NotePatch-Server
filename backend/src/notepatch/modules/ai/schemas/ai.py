from datetime import datetime

from pydantic import BaseModel, Field

from notepatch.shared.schemas import ORMModel


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    conversation_id: str | None = None
    input: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)


class ChatConversationRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str
    title: str
    last_message_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ChatConversationPage(BaseModel):
    items: list[ChatConversationRead]
    page: int
    page_size: int
    total: int


class ChatConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class ChatMessageRead(ORMModel):
    id: str
    workspace_id: str
    conversation_id: str
    user_id: str
    role: str
    content: str
    task_id: str | None = None
    status: str
    error_message: str | None = None
    citations: list[dict] = Field(default_factory=list)
    source_status: str = "available"
    created_at: datetime
    updated_at: datetime


class ChatMessagePage(BaseModel):
    items: list[ChatMessageRead]
    page: int
    page_size: int
    total: int


class GenerateFlashcardsRequest(BaseModel):
    document_id: str | None = None
    subject: str | None = None
    options: dict = Field(default_factory=dict)
