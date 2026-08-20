import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_task_service, get_workspace_member
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db
from notepatch.platform.metrics import SSE_CONNECTIONS
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.identity.models.user import User
from notepatch.modules.ai.models.chat import ChatConversation
from notepatch.modules.ai.services.chat import ChatService
from notepatch.modules.tasks.schemas.task import TaskEventRead, TaskRead
from notepatch.modules.tasks.services.task import TaskService

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


@router.post("/{task_id}/cancel", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
def cancel_chat_task(
    workspace_id: str,
    task_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
) -> Task:
    task = task_service.db.scalar(
        select(Task)
        .join(
            ChatConversation,
            (Task.resource_type == "chat_conversation") & (Task.resource_id == ChatConversation.id),
        )
        .where(
            Task.workspace_id == workspace_id,
            Task.id == task_id,
            Task.task_type == "openclaw_agent_run",
            ChatConversation.workspace_id == workspace_id,
            ChatConversation.user_id == current_user.id,
            ChatConversation.deleted_at.is_(None),
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat task not found")
    if task.status in {"queued", "running"}:
        task_service.request_cancel(task, "Cancelled by user")
        task_service.db.refresh(task)
    if task.status == "cancelled":
        ChatService(task_service.db).mark_assistant_cancelled(task, "Cancelled by user")
        task_service.db.commit()
        task_service.db.refresh(task)
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
        .order_by(TaskEvent.sequence_no.asc())
    ).all()


@router.get("/{task_id}/events/stream")
async def stream_task_events(
    workspace_id: str,
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    task_exists = db.scalar(select(Task.id).where(Task.workspace_id == workspace_id, Task.id == task_id))
    if task_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    try:
        after_sequence = max(0, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Last-Event-ID") from exc
    settings = get_settings()

    async def events():
        sequence = after_sequence
        last_heartbeat = time.monotonic()
        SSE_CONNECTIONS.inc()
        try:
            while True:
                if await request.is_disconnected():
                    return
                db.rollback()
                task = db.scalar(
                    select(Task).where(Task.workspace_id == workspace_id, Task.id == task_id)
                )
                if task is None:
                    return
                rows = db.scalars(
                    select(TaskEvent)
                    .where(
                        TaskEvent.workspace_id == workspace_id,
                        TaskEvent.task_id == task_id,
                        TaskEvent.sequence_no > sequence,
                    )
                    .order_by(TaskEvent.sequence_no.asc())
                ).all()
                for row in rows:
                    sequence = row.sequence_no
                    payload = {
                        "id": row.id,
                        "task_id": row.task_id,
                        "sequence_no": row.sequence_no,
                        "event_type": row.event_type,
                        "level": row.level,
                        "message": row.message,
                        "progress": row.progress,
                        "data": row.data,
                        "created_at": row.created_at.isoformat(),
                    }
                    yield f"id: {row.sequence_no}\nevent: task_event\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if task.status in {"succeeded", "failed", "cancelled"} and not rows:
                    done = {"task_id": task.id, "status": task.status, "last_sequence_no": sequence}
                    yield f"event: done\ndata: {json.dumps(done)}\n\n"
                    return
                now = time.monotonic()
                if now - last_heartbeat >= settings.task_sse_heartbeat_seconds:
                    last_heartbeat = now
                    yield ": heartbeat\n\n"
                await asyncio.sleep(settings.task_sse_poll_seconds)
        finally:
            SSE_CONNECTIONS.dec()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
