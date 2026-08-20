from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class ProfileRead(BaseModel):
    id: str
    name: str | None
    email: EmailStr
    avatar_url: str | None
    profile_version: int
    reauthentication_required: bool = False


class ProfileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    current_password: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("name contains control characters")
        return normalized

    @model_validator(mode="after")
    def require_profile_field(self):
        if not ({"name", "email"} & self.model_fields_set):
            raise ValueError("at least one of name or email must be provided")
        return self


class AvatarRead(BaseModel):
    avatar_url: str | None
    mime_type: str | None
    file_size: int | None
    profile_version: int


class AvatarDownloadRead(AvatarRead):
    download_url: str
    expires_in: int


class MutationReplay(BaseModel):
    data: dict[str, Any]
    replayed: bool
