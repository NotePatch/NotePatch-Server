from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Homework, HomeworkReference
from notepatch.modules.identity.models.user import User
from notepatch.modules.tasks.services.task import TaskService


class HomeworkService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_homework(
        self,
        *,
        workspace_id: str,
        user: User,
        title: str,
        description: str | None,
        document_id: str | None,
        due_at,
        rubric_text: str | None,
        max_score: float,
        metadata: dict,
    ) -> Homework:
        if document_id is not None:
            document = self.db.scalar(
                select(Document).where(
                    Document.workspace_id == workspace_id,
                    Document.id == document_id,
                    Document.status != "deleted",
                )
            )
            if document is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        homework = Homework(
            workspace_id=workspace_id,
            title=title,
            description=description,
            document_id=document_id,
            due_at=due_at,
            rubric_text=rubric_text,
            max_score=max_score,
            metadata_=metadata,
            created_by_user_id=user.id,
        )
        self.db.add(homework)
        self.db.commit()
        self.db.refresh(homework)
        return homework

    def update_grading_config(
        self,
        workspace_id: str,
        homework_id: str,
        *,
        rubric_text: str | None,
        max_score: float | None,
        fields_set: set[str],
    ) -> Homework:
        homework = self.get_homework(workspace_id, homework_id)
        self._cancel_active_grading(homework, "Homework grading configuration changed")
        if "rubric_text" in fields_set:
            homework.rubric_text = rubric_text.strip() if isinstance(rubric_text, str) and rubric_text.strip() else None
        if "max_score" in fields_set and max_score is not None:
            homework.max_score = max_score
        self.db.commit()
        self.db.refresh(homework)
        return homework

    def list_references(self, workspace_id: str, homework_id: str) -> list[HomeworkReference]:
        self.get_homework(workspace_id, homework_id)
        return self.db.scalars(
            select(HomeworkReference)
            .where(
                HomeworkReference.workspace_id == workspace_id,
                HomeworkReference.homework_id == homework_id,
            )
            .order_by(HomeworkReference.created_at.asc())
        ).all()

    def add_reference(
        self,
        workspace_id: str,
        homework_id: str,
        *,
        document_id: str,
        reference_type: str,
    ) -> HomeworkReference:
        self.get_homework(workspace_id, homework_id)
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.id == document_id,
                Document.status != "deleted",
            )
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference document not found")
        if document.document_kind != reference_type:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Reference document_kind must be {reference_type}",
            )
        existing = self.db.scalar(
            select(HomeworkReference).where(
                HomeworkReference.workspace_id == workspace_id,
                HomeworkReference.homework_id == homework_id,
                HomeworkReference.document_id == document_id,
                HomeworkReference.reference_type == reference_type,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Homework reference already exists")
        reference = HomeworkReference(
            workspace_id=workspace_id,
            homework_id=homework_id,
            document_id=document_id,
            reference_type=reference_type,
        )
        self._cancel_active_grading(self.get_homework(workspace_id, homework_id), "Homework reference added")
        self.db.add(reference)
        self.db.commit()
        self.db.refresh(reference)
        return reference

    def delete_reference(self, workspace_id: str, homework_id: str, reference_id: str) -> None:
        reference = self.db.scalar(
            select(HomeworkReference).where(
                HomeworkReference.id == reference_id,
                HomeworkReference.workspace_id == workspace_id,
                HomeworkReference.homework_id == homework_id,
            )
        )
        if reference is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework reference not found")
        homework = self.get_homework(workspace_id, homework_id)
        self._cancel_active_grading(homework, "Homework reference removed")
        self.db.delete(reference)
        self.db.commit()

    def validate_grading_inputs(self, homework: Homework) -> None:
        if not homework.document_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Homework has no source document")
        self._require_ocr_document(homework.workspace_id, homework.document_id, "Homework document")
        references = self.list_references(homework.workspace_id, homework.id)
        for reference in references:
            self._require_ocr_document(homework.workspace_id, reference.document_id, "Reference document")

    def _require_ocr_document(self, workspace_id: str, document_id: str, label: str) -> Document:
        document = self.db.scalar(
            select(Document).where(
                Document.workspace_id == workspace_id,
                Document.id == document_id,
                Document.status == "ready",
            )
        )
        if document is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{label} is not ready")
        has_ocr = self.db.scalar(
            select(DocumentArtifact.id).where(
                DocumentArtifact.workspace_id == workspace_id,
                DocumentArtifact.document_id == document_id,
                DocumentArtifact.artifact_type == "ocr_json",
            )
        )
        if has_ocr is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{label} has no OCR result")
        return document

    def _cancel_active_grading(self, homework: Homework, reason: str) -> None:
        TaskService(self.db).cancel_active_tasks(
            workspace_id=homework.workspace_id,
            resource_type="homework",
            resource_id=homework.id,
            task_types=("grade_homework",),
            reason=reason,
            commit=False,
        )

    def list_grading_results(self, workspace_id: str, homework_id: str) -> list[GradingResult]:
        self.get_homework(workspace_id, homework_id)
        return self.db.scalars(
            select(GradingResult)
            .where(
                GradingResult.workspace_id == workspace_id,
                GradingResult.homework_id == homework_id,
            )
            .order_by(GradingResult.created_at.desc())
        ).all()

    def get_homework(self, workspace_id: str, homework_id: str) -> Homework:
        homework = self.db.scalar(
            select(Homework)
            .options(selectinload(Homework.grading_results))
            .where(Homework.workspace_id == workspace_id, Homework.id == homework_id)
        )
        if homework is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
        if homework.document_id is not None:
            source_status = self.db.scalar(
                select(Document.status).where(
                    Document.workspace_id == workspace_id,
                    Document.id == homework.document_id,
                )
            )
            if source_status == "deleted":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Homework not found")
        return homework
