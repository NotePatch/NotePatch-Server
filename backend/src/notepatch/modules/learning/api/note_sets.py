from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_workspace_member
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.note_workflow import (
    NoteSetCreate, NoteSetDocumentRead, NoteSetRead,
)
from notepatch.modules.learning.services.note_sets import NoteSetService
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.learning.models.learning import LearningUnit
from notepatch.platform.database import get_db

router = APIRouter(prefix="/workspaces/{workspace_id}/note-sets", tags=["note-sets"])


def _read(service: NoteSetService, note_set) -> NoteSetRead:
    result = NoteSetRead.model_validate(note_set)
    result.documents = [NoteSetDocumentRead.model_validate(item) for item in service.documents(note_set.workspace_id, note_set.id)]
    return result


@router.post("", response_model=NoteSetRead, status_code=status.HTTP_201_CREATED)
def create_note_set(
    workspace_id: str,
    payload: NoteSetCreate,
    _member: WorkspaceMember = Depends(get_workspace_member),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NoteSetRead:
    service = NoteSetService(db)
    note_set = service.create(
        workspace_id=workspace_id, user=user, title=payload.title,
        expected_page_count=payload.expected_page_count, learning_unit_id=payload.learning_unit_id,
        subject=payload.subject, grade_level=payload.grade_level, topic=payload.topic,
        content_edit_level=payload.content_edit_level, layout_edit_level=payload.layout_edit_level,
    )
    return _read(service, note_set)


@router.get("/{note_set_id}", response_model=NoteSetRead)
def get_note_set(
    workspace_id: str, note_set_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> NoteSetRead:
    service = NoteSetService(db)
    return _read(service, service.get(workspace_id, note_set_id))


@router.post("/{note_set_id}/complete", response_model=NoteSetRead)
def complete_note_set(
    workspace_id: str, note_set_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> NoteSetRead:
    service = NoteSetService(db)
    note_set = service.complete(workspace_id, note_set_id)
    documents = service.documents(workspace_id, note_set.id)
    if documents:
        ready, _ = service.ready_for_note_generation(workspace_id, documents[0].document_id)
        if ready and note_set.learning_unit_id:
            unit = db.scalar(
                select(LearningUnit).where(
                    LearningUnit.workspace_id == workspace_id,
                    LearningUnit.id == note_set.learning_unit_id,
                )
            )
            if unit is not None:
                note_set.status = "processing"
                db.commit()
                LearningWorkflowService(db).schedule_study_notes(
                    unit,
                    reason="note_set_completed",
                    content_edit_level=note_set.content_edit_level,
                    layout_edit_level=note_set.layout_edit_level,
                )
    return _read(service, note_set)
