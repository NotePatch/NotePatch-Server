from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from notepatch.entrypoints.deps import get_current_user, get_task_service, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import require_member_permission
from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.homework import Homework
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.homework import (
    GradeHomeworkRequest,
    GradingConfigUpdate,
    GradingResultRead,
    HomeworkCreate,
    HomeworkRead,
    HomeworkReferenceCreate,
    HomeworkReferenceRead,
)
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.learning.services.homework import HomeworkService
from notepatch.modules.tasks.services.task import TaskService

router = APIRouter(prefix="/workspaces/{workspace_id}/homeworks", tags=["homeworks"])


@router.get("", response_model=list[HomeworkRead])
def list_homeworks(
    workspace_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[Homework]:
    return db.scalars(
        select(Homework)
        .options(selectinload(Homework.grading_results))
        .outerjoin(Document, Document.id == Homework.document_id)
        .where(
            Homework.workspace_id == workspace_id,
            or_(Homework.document_id.is_(None), Document.status != "deleted"),
        )
        .order_by(Homework.created_at.desc())
    ).all()


@router.post("", response_model=HomeworkRead, status_code=status.HTTP_201_CREATED)
def create_homework(
    workspace_id: str,
    payload: HomeworkCreate,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Homework:
    require_member_permission(db, member, "homeworks.write")
    return HomeworkService(db).create_homework(
        workspace_id=workspace_id,
        user=current_user,
        title=payload.title,
        description=payload.description,
        document_id=payload.document_id,
        due_at=payload.due_at,
        rubric_text=payload.rubric_text,
        max_score=payload.max_score,
        metadata=payload.metadata,
    )


@router.get("/{homework_id}", response_model=HomeworkRead)
def get_homework(
    workspace_id: str,
    homework_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> Homework:
    return HomeworkService(db).get_homework(workspace_id, homework_id)


@router.get("/{homework_id}/grading-results", response_model=list[GradingResultRead])
def list_grading_results(
    workspace_id: str,
    homework_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
):
    return HomeworkService(db).list_grading_results(workspace_id, homework_id)


@router.patch("/{homework_id}/grading-config", response_model=HomeworkRead)
def update_grading_config(
    workspace_id: str,
    homework_id: str,
    payload: GradingConfigUpdate,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> Homework:
    require_member_permission(db, member, "homeworks.write")
    return HomeworkService(db).update_grading_config(
        workspace_id,
        homework_id,
        rubric_text=payload.rubric_text,
        max_score=payload.max_score,
        fields_set=set(payload.model_fields_set),
    )


@router.get("/{homework_id}/references", response_model=list[HomeworkReferenceRead])
def list_homework_references(
    workspace_id: str,
    homework_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list:
    return HomeworkService(db).list_references(workspace_id, homework_id)


@router.post("/{homework_id}/references", response_model=HomeworkReferenceRead, status_code=status.HTTP_201_CREATED)
def add_homework_reference(
    workspace_id: str,
    homework_id: str,
    payload: HomeworkReferenceCreate,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
):
    require_member_permission(db, member, "homeworks.write")
    return HomeworkService(db).add_reference(
        workspace_id,
        homework_id,
        document_id=payload.document_id,
        reference_type=payload.reference_type,
    )


@router.delete("/{homework_id}/references/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_homework_reference(
    workspace_id: str,
    homework_id: str,
    reference_id: str,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> None:
    require_member_permission(db, member, "homeworks.write")
    HomeworkService(db).delete_reference(workspace_id, homework_id, reference_id)


@router.post("/{homework_id}/grade", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def grade_homework(
    workspace_id: str,
    homework_id: str,
    payload: GradeHomeworkRequest,
    member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    require_member_permission(db, member, "homeworks.write")
    homework_service = HomeworkService(db)
    homework = homework_service.get_homework(workspace_id, homework_id)
    if payload.student_user_id is not None and payload.student_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="student_user_id must be the personal workspace owner")
    homework_service.validate_grading_inputs(homework)
    active = task_service.find_active_task(
        workspace_id=workspace_id,
        task_type="grade_homework",
        resource_type="homework",
        resource_id=homework.id,
    )
    if active is not None:
        return active
    return task_service.create_task(
        workspace_id=workspace_id,
        task_type="grade_homework",
        resource_type="homework",
        resource_id=homework.id,
        payload={"homework_id": homework.id, "student_user_id": current_user.id, "options": payload.options},
    )
