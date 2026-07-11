from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import require_member_permission
from notepatch.modules.learning.models.homework import Mistake
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.homework import MistakeRead, MistakeUpdate

router = APIRouter(prefix="/workspaces/{workspace_id}/mistakes", tags=["mistakes"])


@router.get("", response_model=list[MistakeRead])
def list_mistakes(
    workspace_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> list[Mistake]:
    return db.scalars(select(Mistake).where(Mistake.workspace_id == workspace_id).order_by(Mistake.created_at.desc())).all()


@router.patch("/{mistake_id}", response_model=MistakeRead)
def update_mistake(
    workspace_id: str,
    mistake_id: str,
    payload: MistakeUpdate,
    member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> Mistake:
    require_member_permission(db, member, "mistakes.write")
    mistake = db.scalar(select(Mistake).where(Mistake.workspace_id == workspace_id, Mistake.id == mistake_id))
    if mistake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mistake not found")
    if payload.status is not None:
        mistake.status = payload.status
    if payload.description is not None:
        mistake.description = payload.description
    if payload.metadata is not None:
        mistake.metadata_ = {**(mistake.metadata_ or {}), **payload.metadata}
    db.commit()
    db.refresh(mistake)
    return mistake
