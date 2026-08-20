from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from notepatch.shared.schemas import ORMModel


THINKING_EFFORTS = {"minimal", "low", "medium", "high", "adaptive"}


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    client_locale: str | None = Field(
        default=None,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    )
    conversation_id: str | None = None
    input: dict = Field(default_factory=dict)
    options: dict = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def normalize_thinking_options(cls, value: dict) -> dict:
        options = dict(value)
        temperature = options.get("temperature")
        if temperature is not None:
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                raise ValueError("options.temperature must be a number")
            if not 0 <= float(temperature) <= 2:
                raise ValueError("options.temperature must be between 0 and 2")
            options["temperature"] = float(temperature)
        raw_thinking = options.get("thinking")
        if raw_thinking is None:
            options["thinking"] = {"enabled": False, "effort": "off"}
            return options
        if not isinstance(raw_thinking, dict):
            raise ValueError("options.thinking must be an object")
        enabled = raw_thinking.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("options.thinking.enabled must be a boolean")
        raw_effort = raw_thinking.get("effort", "low")
        if not isinstance(raw_effort, str):
            raise ValueError("options.thinking.effort must be a string")
        effort = raw_effort.strip().lower()
        if enabled and effort not in THINKING_EFFORTS:
            raise ValueError("options.thinking.effort must be minimal, low, medium, high, or adaptive")
        options["thinking"] = {"enabled": enabled, "effort": effort if enabled else "off"}
        return options


class ChatMessageRevisionRequest(BaseModel):
    prompt: str = Field(min_length=1)
    client_locale: str | None = Field(
        default=None,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
    )
    input: dict | None = None
    options: dict = Field(default_factory=dict)

    @field_validator("options")
    @classmethod
    def normalize_options(cls, value: dict) -> dict:
        return ChatRequest.normalize_thinking_options(value)


class ChatConversationRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str
    title: str
    title_source: str = "prompt"
    title_generated_at: datetime | None = None
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
    attachments: list[dict] = Field(default_factory=list)
    citations: list[dict] = Field(default_factory=list)
    source_status: str = "available"
    model_id: str | None = None
    revision_of_message_id: str | None = None
    superseded_by_message_id: str | None = None
    superseded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ChatMessagePage(BaseModel):
    items: list[ChatMessageRead]
    page: int
    page_size: int
    total: int


class AiModelRead(BaseModel):
    id: str
    upstream_id: str
    owned_by: str | None = None
    created: int | None = None


class AiModelCatalogRead(BaseModel):
    provider: str
    default_model: str
    selected_model: str
    items: list[AiModelRead]
    fetched_at: datetime
    stale: bool


class AiModelSelectionUpdate(BaseModel):
    model_id: str | None = Field(max_length=255)


class AiModelSelectionRead(BaseModel):
    selected_model: str
    preferred_model: str | None
    default_model: str


class GenerateFlashcardsRequest(BaseModel):
    learning_unit_id: str | None = None
    document_id: str | None = None
    subject: str | None = None
    options: dict = Field(default_factory=dict)
