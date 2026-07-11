from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.services.permissions import get_role
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from notepatch.modules.identity.schemas.workspace import WorkspaceCreate, WorkspaceMemberCreate, WorkspaceMemberRead, WorkspaceRead

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Workspace]:
    return db.scalars(
        select(Workspace)
        .where(Workspace.owner_user_id == current_user.id, Workspace.type == "personal")
        .order_by(Workspace.created_at.asc())
    ).all()


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Workspace:
    existing_workspace_id = db.scalar(select(Workspace.id).where(Workspace.owner_user_id == current_user.id))
    if existing_workspace_id is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Personal workspace already exists")

    owner_role = get_role(db, "owner")
    workspace = Workspace(name=payload.name, type="personal", owner_user_id=current_user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=current_user.id, role_id=owner_role.id))
    db.commit()
    db.refresh(workspace)
    return workspace


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(
    workspace_id: str,
    _member: WorkspaceMember = Depends(get_workspace_member),
    db: Session = Depends(get_db),
) -> Workspace:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    return workspace


@router.post("/{workspace_id}/members", response_model=WorkspaceMemberRead, status_code=status.HTTP_201_CREATED)
def add_workspace_member(
    workspace_id: str,
    payload: WorkspaceMemberCreate,
    _member: WorkspaceMember = Depends(get_workspace_member),
) -> WorkspaceMember:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Workspace members are disabled for personal workspaces",
    )
