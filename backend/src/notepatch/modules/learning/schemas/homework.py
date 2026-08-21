from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from notepatch.shared.schemas import ORMModel, metadata_field


class HomeworkCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    document_id: str | None = None
    due_at: datetime | None = None
    rubric_text: str | None = None
    max_score: float = Field(default=100.0, gt=0)
    metadata: dict = Field(default_factory=dict)


class GradingResultRead(ORMModel):
    id: str
    workspace_id: str
    homework_id: str
    question_id: str | None = None
    student_user_id: str | None = None
    score: float | None = None
    max_score: float | None = None
    grading_mode: str
    confidence: float | None = None
    feedback: str | None = None
    created_at: datetime


class HomeworkRead(ORMModel):
    id: str
    workspace_id: str
    title: str
    description: str | None = None
    document_id: str | None = None
    due_at: datetime | None = None
    status: str
    rubric_text: str | None = None
    max_score: float
    metadata: dict = metadata_field()
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime
    latest_grading_result: GradingResultRead | None = None


class QuestionRead(ORMModel):
    id: str
    workspace_id: str
    document_id: str | None = None
    homework_id: str | None = None
    sequence_no: int
    question_type: str | None = None
    prompt: str
    answer: str | None = None
    metadata: dict = metadata_field()
    created_at: datetime


class GradeHomeworkRequest(BaseModel):
    student_user_id: str | None = None
    options: dict = Field(default_factory=dict)


class GradingConfigUpdate(BaseModel):
    rubric_text: str | None = None
    max_score: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def require_at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("At least one grading configuration field is required")
        return self


class HomeworkReferenceCreate(BaseModel):
    document_id: str
    reference_type: str = Field(pattern="^(answer_key|rubric)$")


class HomeworkReferenceRead(ORMModel):
    id: str
    workspace_id: str
    homework_id: str
    document_id: str
    reference_type: str
    created_at: datetime


class MistakeRead(ORMModel):
    id: str
    workspace_id: str
    question_id: str | None = None
    knowledge_point_id: str | None = None
    grading_result_id: str | None = None
    student_user_id: str | None = None
    subject: str | None = None
    knowledge_point: str | None = None
    description: str
    status: str
    metadata: dict = metadata_field()
    created_at: datetime
    updated_at: datetime


class MistakeUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(open|resolved|ignored)$")
    description: str | None = None
    metadata: dict | None = None
