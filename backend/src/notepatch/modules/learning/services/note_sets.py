from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document
from notepatch.modules.identity.models.user import User
from notepatch.modules.learning.models.learning import LearningUnit
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.note_workflow import NoteSet, NoteSetDocument
from notepatch.platform.database import utcnow


class NoteSetService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        workspace_id: str,
        user: User,
        title: str,
        expected_page_count: int,
        learning_unit_id: str | None,
        subject: str | None,
        grade_level: str | None,
        topic: str | None,
        content_edit_level: str | None,
        layout_edit_level: str | None,
    ) -> NoteSet:
        unit = None
        if learning_unit_id:
            unit = self.db.scalar(select(LearningUnit).where(
                LearningUnit.workspace_id == workspace_id,
                LearningUnit.id == learning_unit_id,
                LearningUnit.merged_into_id.is_(None),
            ))
            if unit is None:
                raise HTTPException(status_code=404, detail="Learning unit not found")
        else:
            unit = LearningUnit(
                workspace_id=workspace_id,
                title=title.strip(),
                subject=subject,
                grade_level=grade_level,
                topic=topic,
                metadata_={"source": "note_set", "automatic_pipeline": True},
            )
            self.db.add(unit)
            self.db.flush()
        note_set = NoteSet(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            user_id=user.id,
            learning_unit_id=unit.id,
            title=title.strip(),
            expected_page_count=expected_page_count,
            content_edit_level=content_edit_level or user.note_content_edit_level,
            layout_edit_level=layout_edit_level or user.note_layout_edit_level,
            metadata_={"subject": subject, "grade_level": grade_level, "topic": topic},
        )
        self.db.add(note_set)
        self.db.commit()
        self.db.refresh(note_set)
        return note_set

    def get(self, workspace_id: str, note_set_id: str, *, lock: bool = False) -> NoteSet:
        query = select(NoteSet).where(NoteSet.workspace_id == workspace_id, NoteSet.id == note_set_id)
        if lock:
            query = query.with_for_update()
        note_set = self.db.scalar(query)
        if note_set is None:
            raise HTTPException(status_code=404, detail="Note set not found")
        return note_set

    def documents(self, workspace_id: str, note_set_id: str) -> list[NoteSetDocument]:
        return self.db.scalars(select(NoteSetDocument).where(
            NoteSetDocument.workspace_id == workspace_id,
            NoteSetDocument.note_set_id == note_set_id,
        ).order_by(NoteSetDocument.page_index)).all()

    def validate_upload(self, workspace_id: str, note_set_id: str, page_index: int, document_kind: str) -> NoteSet:
        if document_kind != "note":
            raise HTTPException(status_code=422, detail="note_set_id is only valid for note documents")
        note_set = self.get(workspace_id, note_set_id, lock=True)
        if note_set.status != "open":
            raise HTTPException(status_code=409, detail="Note set is already completed")
        if page_index < 0 or page_index >= note_set.expected_page_count:
            raise HTTPException(status_code=422, detail="page_index is outside the note set range")
        occupied = self.db.scalar(select(NoteSetDocument.id).where(
            NoteSetDocument.workspace_id == workspace_id,
            NoteSetDocument.note_set_id == note_set_id, NoteSetDocument.page_index == page_index
        ))
        if occupied is not None:
            raise HTTPException(status_code=409, detail="Note set page is already occupied")
        return note_set

    def attach(self, note_set: NoteSet, document: Document, page_index: int) -> None:
        document.metadata_ = {
            **(document.metadata_ or {}),
            "note_set_id": note_set.id,
            "note_set_page_index": page_index,
            "learning_unit_id": note_set.learning_unit_id,
            "note_content_edit_level": note_set.content_edit_level,
            "note_layout_edit_level": note_set.layout_edit_level,
        }
        self.db.add(NoteSetDocument(
            workspace_id=document.workspace_id, note_set_id=note_set.id, document_id=document.id, page_index=page_index
        ))
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Note set page is already occupied") from exc

    def complete(self, workspace_id: str, note_set_id: str) -> NoteSet:
        note_set = self.get(workspace_id, note_set_id, lock=True)
        if note_set.status in {"completed", "processing", "ready"}:
            return note_set
        rows = self.db.execute(
            select(NoteSetDocument, Document)
            .join(Document, Document.id == NoteSetDocument.document_id)
            .where(NoteSetDocument.workspace_id == workspace_id, NoteSetDocument.note_set_id == note_set.id)
        ).all()
        if len(rows) != note_set.expected_page_count:
            raise HTTPException(status_code=409, detail="Not all note set pages have been uploaded")
        if any(document.status not in {"uploaded", "processing", "ready"} for _, document in rows):
            raise HTTPException(status_code=409, detail="Not all note set uploads are complete")
        indexes = sorted(link.page_index for link, _ in rows)
        if indexes != list(range(note_set.expected_page_count)):
            raise HTTPException(status_code=409, detail="Note set page indexes are incomplete")
        note_set.status = "completed"
        note_set.completed_at = utcnow()
        self.db.commit()
        self.db.refresh(note_set)
        return note_set

    def ready_for_note_generation(self, workspace_id: str, document_id: str) -> tuple[bool, NoteSet | None]:
        link = self.db.scalar(select(NoteSetDocument).where(
            NoteSetDocument.workspace_id == workspace_id, NoteSetDocument.document_id == document_id
        ))
        if link is None:
            return True, None
        note_set = self.get(workspace_id, link.note_set_id)
        if note_set.status not in {"completed", "processing", "ready"}:
            return False, note_set
        document_ids = self.db.scalars(
            select(NoteSetDocument.document_id).where(
                NoteSetDocument.workspace_id == workspace_id,
                NoteSetDocument.note_set_id == note_set.id,
            )
        ).all()
        if len(document_ids) != note_set.expected_page_count:
            return False, note_set
        knowledge_documents = self.db.scalar(
            select(func.count(func.distinct(KnowledgeChunk.document_id))).where(
                KnowledgeChunk.workspace_id == workspace_id,
                KnowledgeChunk.document_id.in_(document_ids),
            )
        ) or 0
        return knowledge_documents == note_set.expected_page_count, note_set
