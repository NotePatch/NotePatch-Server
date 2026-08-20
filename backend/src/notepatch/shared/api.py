from typing import Generic, TypeVar

from pydantic import BaseModel


T = TypeVar("T")


class ApiEnvelope(BaseModel, Generic[T]):
    code: str
    message: str
    data: T | None = None


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        data: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data
        self.headers = headers or {}
