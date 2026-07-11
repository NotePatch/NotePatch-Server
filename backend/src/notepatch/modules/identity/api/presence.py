from fastapi import APIRouter, Depends

from notepatch.entrypoints.deps import get_current_user, get_presence_service
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.schemas.auth import OkResponse
from notepatch.modules.identity.schemas.presence import PresenceHeartbeatRequest, PresenceHeartbeatResponse, PresenceOfflineRequest
from notepatch.modules.identity.services.presence import PresenceService

router = APIRouter(prefix="/presence", tags=["presence"])


@router.post("/heartbeat", response_model=PresenceHeartbeatResponse)
def heartbeat(
    payload: PresenceHeartbeatRequest,
    current_user: User = Depends(get_current_user),
    presence: PresenceService = Depends(get_presence_service),
) -> dict:
    return presence.heartbeat(current_user.id, payload.client_id)


@router.post("/offline", response_model=OkResponse)
def offline(
    payload: PresenceOfflineRequest,
    current_user: User = Depends(get_current_user),
    presence: PresenceService = Depends(get_presence_service),
) -> OkResponse:
    presence.offline(current_user.id, payload.client_id)
    return OkResponse()
