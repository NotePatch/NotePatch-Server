"""Aggregate SQLAlchemy models for metadata registration and Alembic."""

from notepatch.modules.ai.models.chat import ChatConversation, ChatMessage
from notepatch.modules.admin.models.admin import AdminAuditLog, AdminOperation
from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.documents.models.upload import UploadSession
from notepatch.modules.identity.models.user import IdentityAuditLog, IdentityMutationKey, RefreshToken, User
from notepatch.modules.identity.models.workspace import (
    Permission,
    Role,
    RolePermission,
    Workspace,
    WorkspaceMember,
)
from notepatch.modules.learning.models.homework import (
    GradingResult,
    Homework,
    HomeworkReference,
    Mistake,
    Question,
)
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePoint,
    KnowledgePointAttempt,
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.learning.models.assignment import LearningUnitAssignment
from notepatch.modules.tasks.models.task import Task, TaskEvent
from notepatch.modules.tasks.models.workflow import WorkflowEvent, WorkflowRun, WorkflowTaskLink

__all__ = [
    "ChatConversation",
    "ChatMessage",
    "AdminAuditLog",
    "AdminOperation",
    "Document",
    "DocumentArtifact",
    "GradingResult",
    "Homework",
    "HomeworkReference",
    "KnowledgeChunk",
    "KnowledgePoint",
    "KnowledgePointAttempt",
    "FlashcardDeck",
    "Flashcard",
    "LearningUnit",
    "LearningUnitAssignment",
    "LearningUnitDocument",
    "Mistake",
    "Permission",
    "Question",
    "IdentityAuditLog",
    "IdentityMutationKey",
    "RefreshToken",
    "Role",
    "RolePermission",
    "StudyNoteVersion",
    "Task",
    "TaskEvent",
    "WorkflowEvent",
    "WorkflowRun",
    "WorkflowTaskLink",
    "UploadSession",
    "User",
    "Workspace",
    "WorkspaceMember",
]
