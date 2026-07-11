from datetime import datetime, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_presence_service
from notepatch.platform.database import get_db, utcnow
from notepatch.modules.identity.services.permissions import get_role, seed_roles_and_permissions
from notepatch.platform.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from notepatch.modules.identity.models.user import RefreshToken, User
from notepatch.modules.identity.models.workspace import Workspace, WorkspaceMember
from notepatch.modules.identity.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    OkResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserRead,
    UserPreferencesUpdate,
)
from notepatch.modules.ai.services.runtime import OpenClawUserRuntimeService
from notepatch.modules.identity.services.presence import PresenceService

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.lower().strip()


def _is_expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= utcnow()


def _issue_tokens(db: Session, user: User) -> TokenResponse:
    access_token, access_expires_at = create_access_token(user.id)
    refresh_token, refresh_expires_at = create_refresh_token(user.id)
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=refresh_expires_at,
        )
    )
    db.commit()
    db.refresh(user)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=access_expires_at,
        user=user,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = _normalize_email(payload.email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    seed_roles_and_permissions(db)
    owner_role = get_role(db, "owner")
    user = User(email=email, password_hash=hash_password(payload.password), full_name=payload.full_name)
    db.add(user)
    db.flush()

    workspace_name = f"{payload.full_name or email}'s Workspace"
    workspace = Workspace(name=workspace_name, type="personal", owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role_id=owner_role.id))
    OpenClawUserRuntimeService().provision_user(user, workspace)
    db.commit()
    db.refresh(user)
    return _issue_tokens(db, user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = _normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return _issue_tokens(db, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    token_hash = hash_token(payload.refresh_token)
    refresh_token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if refresh_token is None or refresh_token.revoked_at is not None or _is_expired(refresh_token.expires_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    user = db.get(User, refresh_token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    refresh_token.revoked_at = utcnow()
    db.commit()
    return _issue_tokens(db, user)


@router.post("/logout", response_model=OkResponse)
def logout(
    payload: LogoutRequest,
    db: Session = Depends(get_db),
    presence: PresenceService = Depends(get_presence_service),
) -> OkResponse:
    refresh_token = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token)))
    if refresh_token is not None and refresh_token.revoked_at is None:
        if payload.client_id:
            try:
                presence.offline(refresh_token.user_id, payload.client_id)
            except Exception as exc:
                logger.warning("Could not clear presence for user %s: %s", refresh_token.user_id, exc)
        refresh_token.revoked_at = utcnow()
        db.commit()
    return OkResponse()


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/preferences", response_model=UserRead)
def update_preferences(
    payload: UserPreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    current_user.ai_history_enabled = payload.ai_history_enabled
    db.commit()
    db.refresh(current_user)
    return current_user
