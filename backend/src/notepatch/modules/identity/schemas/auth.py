from datetime import datetime
import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

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
    auto_image_remark_enabled: bool = True
    preferred_ai_model: str | None = None
    ai_onboarding_version: int = 0
    ai_onboarding_completed_at: datetime | None = None
    ai_onboarding_completed: bool = False
    ai_preferences: dict = Field(default_factory=dict)
    note_content_edit_level: str = "conceptual"
    note_layout_edit_level: str = "minor"
    note_history_limit: int = 3
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
    ai_history_enabled: bool | None = None
    auto_image_remark_enabled: bool | None = None
    note_content_edit_level: str | None = Field(
        default=None, pattern=r"^(verbatim|spelling|conceptual|rewrite)$"
    )
    note_layout_edit_level: str | None = Field(
        default=None, pattern=r"^(preserve|minor|reorder|reflow)$"
    )
    note_history_limit: int | None = Field(default=None, ge=0, le=100)
    ai_preferences: "AiPreferencesPatch | None" = None


class AiPreferences(BaseModel):
    response_language: Literal["match_user", "client_locale", "zh-CN", "en-US", "pt-BR"]
    collaboration_style: Literal["direct", "collaborative", "coach", "socratic"]
    response_depth: Literal["concise", "balanced", "detailed"]
    response_structure: Literal["adaptive", "steps", "bullets", "prose"]
    clarification_policy: Literal["ask_when_ambiguous", "assume_when_safe", "confirm_before_actions"]
    feedback_tone: Literal["gentle", "neutral", "strict"]
    learning_guidance: Literal["answer_first", "explain_then_answer", "hint_first"]
    custom_instructions: str | None = Field(default=None, max_length=1000)

    @field_validator("custom_instructions")
    @classmethod
    def normalize_custom_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
        return normalized or None


class AiPreferencesPatch(BaseModel):
    response_language: Literal["match_user", "client_locale", "zh-CN", "en-US", "pt-BR"] | None = None
    collaboration_style: Literal["direct", "collaborative", "coach", "socratic"] | None = None
    response_depth: Literal["concise", "balanced", "detailed"] | None = None
    response_structure: Literal["adaptive", "steps", "bullets", "prose"] | None = None
    clarification_policy: Literal["ask_when_ambiguous", "assume_when_safe", "confirm_before_actions"] | None = None
    feedback_tone: Literal["gentle", "neutral", "strict"] | None = None
    learning_guidance: Literal["answer_first", "explain_then_answer", "hint_first"] | None = None
    custom_instructions: str | None = Field(default=None, max_length=1000)

    @field_validator("custom_instructions")
    @classmethod
    def normalize_custom_instructions(cls, value: str | None) -> str | None:
        return AiPreferences.normalize_custom_instructions(value)


class AiOnboardingOptionRead(BaseModel):
    value: str
    label_key: str


class AiOnboardingQuestionRead(BaseModel):
    id: str
    message_key: str
    required: bool
    options: list[AiOnboardingOptionRead]


class AiOnboardingRead(BaseModel):
    version: int
    completed: bool
    completed_at: datetime | None = None
    answers: AiPreferences
    questions: list[AiOnboardingQuestionRead]


class AiOnboardingUpdate(BaseModel):
    version: int
    answers: AiPreferences


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
