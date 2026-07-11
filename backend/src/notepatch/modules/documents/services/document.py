from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.documents.services.purge import DocumentPurgeService
from notepatch.platform.storage import StorageService


class DocumentService:
    def __init__(self, db: Session, storage: StorageService) -> None:
        self.db = db
        self.storage = storage

    def get_document(self, workspace_id: str, document_id: str, *, include_deleted: bool = False) -> Document:
        query = select(Document).where(Document.workspace_id == workspace_id, Document.id == document_id)
        if not include_deleted:
            query = query.where(Document.status != "deleted")
        document = self.db.scalar(query)
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        return document

    def request_delete(self, workspace_id: str, document_id: str) -> tuple[Document, Task]:
        return DocumentPurgeService(self.db, self.storage).request_purge(workspace_id, document_id)
