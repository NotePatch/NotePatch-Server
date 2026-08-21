import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_workspace_member
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.models.workflow import WorkflowEvent, WorkflowRun, WorkflowTaskLink
from notepatch.modules.tasks.schemas.task import TaskRead
from notepatch.modules.tasks.schemas.workflow import (
    WorkflowDetailResponse,
    WorkflowEventRead,
    WorkflowRunRead,
    WorkflowTaskRead,
)
from notepatch.platform.config import get_settings
from notepatch.platform.database import get_db


router = APIRouter(prefix="/workspaces/{workspace_id}/workflows", tags=["workflows"])
document_router = APIRouter(prefix="/workspaces/{workspace_id}/documents", tags=["workflows"])


def _workflow(db: Session, workspace_id: str, workflow_run_id: str) -> WorkflowRun:
    run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.id == workflow_run_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return run


def _detail(db: Session, run: WorkflowRun) -> WorkflowDetailResponse:
    rows = db.execute(
        select(WorkflowTaskLink, Task)
        .join(Task, Task.id == WorkflowTaskLink.task_id)
        .where(
            WorkflowTaskLink.workflow_run_id == run.id,
            Task.workspace_id == run.workspace_id,
        )
        .order_by(WorkflowTaskLink.created_at.asc())
    ).all()
    return WorkflowDetailResponse(
        workflow=WorkflowRunRead.model_validate(run),
        tasks=[
            WorkflowTaskRead(
                stage=link.stage,
                phase=link.phase,
                required=link.required,
                task=TaskRead.model_validate(task),
            )
            for link, task in rows
        ],
    )


@router.get("", response_model=list[WorkflowRunRead])
def list_workflows(
    workspace_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    status_filter: str | None = Query(default=None, alias="status"),
    document_id: str | None = None,
    learning_unit_id: str | None = None,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[WorkflowRun]:
    query = select(WorkflowRun).where(WorkflowRun.workspace_id == workspace_id)
    if status_filter:
        query = query.where(WorkflowRun.status == status_filter)
    if document_id:
        query = query.where(WorkflowRun.document_id == document_id)
    if learning_unit_id:
        query = query.where(WorkflowRun.learning_unit_id == learning_unit_id)
    return db.scalars(
        query.order_by(WorkflowRun.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()


@router.get("/{workflow_run_id}", response_model=WorkflowDetailResponse)
def get_workflow(
    workspace_id: str,
    workflow_run_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> WorkflowDetailResponse:
    return _detail(db, _workflow(db, workspace_id, workflow_run_id))


@document_router.get("/{document_id}/workflow", response_model=WorkflowDetailResponse)
def get_document_workflow(
    workspace_id: str,
    document_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> WorkflowDetailResponse:
    run = db.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.workspace_id == workspace_id,
            WorkflowRun.document_id == document_id,
        )
        .order_by(WorkflowRun.created_at.desc())
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return _detail(db, run)


@router.get("/{workflow_run_id}/events", response_model=list[WorkflowEventRead])
def get_workflow_events(
    workspace_id: str,
    workflow_run_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[WorkflowEvent]:
    run = _workflow(db, workspace_id, workflow_run_id)
    return db.scalars(
        select(WorkflowEvent)
        .where(
            WorkflowEvent.workspace_id == workspace_id,
            WorkflowEvent.workflow_run_id == run.id,
        )
        .order_by(WorkflowEvent.sequence_no.asc())
    ).all()


@router.get("/{workflow_run_id}/events/stream")
async def stream_workflow_events(
    workspace_id: str,
    workflow_run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    _workflow(db, workspace_id, workflow_run_id)
    try:
        after_sequence = max(0, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Last-Event-ID") from exc
    settings = get_settings()

    async def events():
        sequence = after_sequence
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                return
            db.rollback()
            run = db.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.workspace_id == workspace_id,
                    WorkflowRun.id == workflow_run_id,
                )
            )
            if run is None:
                return
            rows = db.scalars(
                select(WorkflowEvent)
                .where(
                    WorkflowEvent.workspace_id == workspace_id,
                    WorkflowEvent.workflow_run_id == workflow_run_id,
                    WorkflowEvent.sequence_no > sequence,
                )
                .order_by(WorkflowEvent.sequence_no.asc())
            ).all()
            for row in rows:
                sequence = row.sequence_no
                payload = WorkflowEventRead.model_validate(row).model_dump(mode="json")
                yield (
                    f"id: {row.sequence_no}\n"
                    f"event: workflow_event\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
            if run.status in {"succeeded", "partially_succeeded", "failed", "cancelled"} and not rows:
                done = {"workflow_run_id": run.id, "status": run.status, "last_sequence_no": sequence}
                yield f"event: done\ndata: {json.dumps(done)}\n\n"
                return
            now = time.monotonic()
            if now - last_heartbeat >= settings.task_sse_heartbeat_seconds:
                last_heartbeat = now
                yield ": heartbeat\n\n"
            await asyncio.sleep(settings.task_sse_poll_seconds)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
