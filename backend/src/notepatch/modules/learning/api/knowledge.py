from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from notepatch.entrypoints.deps import get_current_user, get_workspace_member
from notepatch.platform.database import get_db
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import WorkspaceMember
from notepatch.modules.learning.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from notepatch.modules.learning.services.knowledge import KnowledgeService


router = APIRouter(prefix="/workspaces/{workspace_id}/knowledge", tags=["knowledge"])


def get_knowledge_service(db: Session = Depends(get_db)) -> KnowledgeService:
    return KnowledgeService(db)


@router.post("/search", response_model=KnowledgeSearchResponse)
def search_knowledge(
    workspace_id: str,
    payload: KnowledgeSearchRequest,
    _member: WorkspaceMember = Depends(get_workspace_member),
    current_user: User = Depends(get_current_user),
    service: KnowledgeService = Depends(get_knowledge_service),
) -> KnowledgeSearchResponse:
    items = service.search(
        workspace_id=workspace_id,
        query=payload.query,
        learning_unit_id=payload.learning_unit_id,
        subject=payload.subject,
        limit=payload.limit,
        owner=f"knowledge-search:{current_user.id}",
    )
    return KnowledgeSearchResponse(items=items)
