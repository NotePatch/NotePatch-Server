from datetime import datetime

from pydantic import BaseModel, Field


class PresenceHeartbeatRequest(BaseModel):
    client_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class PresenceOfflineRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class PresenceHeartbeatResponse(BaseModel):
    client_id: str
    online_until: datetime
    heartbeat_interval_seconds: int
