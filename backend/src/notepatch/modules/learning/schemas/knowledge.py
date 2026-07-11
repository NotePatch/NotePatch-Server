from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    learning_unit_id: str | None = None
    subject: str | None = None
    limit: int = Field(default=6, ge=1, le=20)


class KnowledgeSearchItem(BaseModel):
    id: str
    workspace_id: str
    document_id: str | None = None
    subject: str | None = None
    grade_level: str | None = None
    source_type: str | None = None
    content: str
    metadata: dict
    score: float
    created_at: datetime


class KnowledgeSearchResponse(BaseModel):
    items: list[KnowledgeSearchItem]
