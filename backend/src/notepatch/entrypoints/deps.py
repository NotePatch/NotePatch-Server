from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.platform.database import get_db
from notepatch.platform.security import decode_token
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from notepatch.platform.storage import StorageService
from notepatch.modules.identity.services.presence import PresenceService
from notepatch.modules.tasks.services.task import TaskService

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = decode_token(credentials.credentials, expected_type="access")
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


def get_workspace_member(
    workspace_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WorkspaceMember:
    workspace = db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    if workspace.type != "personal" or workspace.owner_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your personal workspace")

    member = db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == current_user.id,
        )
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of this workspace")
    if member.role is None or member.role.name != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Personal workspace owner access required")
    return member


def get_storage_service() -> StorageService:
    return StorageService()


def get_presence_service() -> PresenceService:
    return PresenceService()


def get_task_service(db: Session = Depends(get_db)) -> TaskService:
    return TaskService(db)
