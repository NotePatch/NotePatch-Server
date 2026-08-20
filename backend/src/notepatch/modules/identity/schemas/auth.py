from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from notepatch.shared.schemas import ORMModel


class UserRead(ORMModel):
    id: str
    email: EmailStr
    phone: str | None = None
    username: str | None = None
    full_name: str | None = None
    is_active: bool
    must_change_password: bool
    ai_history_enabled: bool
    preferred_ai_model: str | None = None
    avatar_url: str | None = None
    profile_version: int = 1
    created_at: datetime


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str
    client_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class UserPreferencesUpdate(BaseModel):
    ai_history_enabled: bool


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserRead


class OkResponse(BaseModel):
    ok: bool = True
