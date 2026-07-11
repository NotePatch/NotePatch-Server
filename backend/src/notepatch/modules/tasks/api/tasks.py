from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.tasks.schemas.task import TaskEventRead, TaskRead

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    workspace_id: str,
    task_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> Task:
    task = db.scalar(select(Task).where(Task.workspace_id == workspace_id, Task.id == task_id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.get("/{task_id}/events", response_model=list[TaskEventRead])
def get_task_events(
    workspace_id: str,
    task_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[TaskEvent]:
    task_exists = db.scalar(select(Task.id).where(Task.workspace_id == workspace_id, Task.id == task_id))
    if task_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return db.scalars(
        select(TaskEvent)
        .where(TaskEvent.workspace_id == workspace_id, TaskEvent.task_id == task_id)
        .order_by(TaskEvent.created_at.asc())
    ).all()
