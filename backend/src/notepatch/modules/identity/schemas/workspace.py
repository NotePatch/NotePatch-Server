from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from notepatch.modules.identity.schemas.auth import UserRead
from notepatch.shared.schemas import ORMModel


class RoleRead(ORMModel):
    id: str
    name: str
    description: str | None = None


class WorkspaceRead(ORMModel):
    id: str
    name: str
    type: Literal["personal"]
    owner_user_id: str
    created_at: datetime
    updated_at: datetime


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceMemberRead(ORMModel):
    id: str
    workspace_id: str
    user_id: str
    role: RoleRead
    user: UserRead
    created_at: datetime


class WorkspaceMemberCreate(BaseModel):
    email: EmailStr
