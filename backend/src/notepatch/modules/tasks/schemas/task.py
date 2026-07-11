from datetime import datetime

from pydantic import BaseModel

from notepatch.shared.schemas import ORMModel


class TaskRead(ORMModel):
    id: str
    workspace_id: str
    task_type: str
    status: str
    resource_type: str | None = None
    resource_id: str | None = None
    payload: dict
    result: dict | None = None
    error_message: str | None = None
    progress: int
    attempt: int
    max_attempts: int
    next_attempt_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class TaskEventRead(ORMModel):
    id: str
    workspace_id: str
    task_id: str
    event_type: str
    level: str
    message: str
    progress: int | None = None
    data: dict
    created_at: datetime


class TaskCreateResponse(BaseModel):
    task: TaskRead
