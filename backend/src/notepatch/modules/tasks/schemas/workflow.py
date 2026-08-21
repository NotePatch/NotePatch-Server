from datetime import datetime

from pydantic import BaseModel, Field

from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.shared.schemas import ORMModel, metadata_field


class WorkflowRunRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str | None = None
    document_id: str | None = None
    learning_unit_id: str | None = None
    trigger_type: str
    status: str
    core_status: str
    enrichment_status: str
    current_stage: str | None = None
    progress: int
    waiting_until: datetime | None = None
    error_message: str | None = None
    result: dict = Field(default_factory=dict)
    metadata: dict = metadata_field()
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowTaskRead(BaseModel):
    stage: str
    phase: str
    required: bool
    task: TaskRead


class WorkflowDetailResponse(BaseModel):
    workflow: WorkflowRunRead
    tasks: list[WorkflowTaskRead] = Field(default_factory=list)


class WorkflowEventRead(ORMModel):
    id: str
    workspace_id: str
    workflow_run_id: str
    task_id: str | None = None
    task_event_id: str | None = None
    sequence_no: int
    stage: str | None = None
    event_type: str
    level: str
    message: str
    progress: int | None = None
    data: dict = Field(default_factory=dict)
    created_at: datetime
