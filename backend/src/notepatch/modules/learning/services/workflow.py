from __future__ import annotations

import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import (
    AUTO_LEARNING_DOCUMENT_KINDS,
    CHAT_ATTACHMENT_KIND,
    Document,
    DocumentArtifact,
)
from notepatch.modules.learning.models.homework import GradingResult, Homework, HomeworkReference, Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument, StudyNoteVersion
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.platform.errors import PermanentTaskError
from notepatch.modules.ai.services.skill_runner import OpenClawSkillRunner
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    GradingSkillResult,
    KnowledgeBuildResult,
    NoteHighlightResult,
    QuestionExtractionResult,
    ScholarNotesResult,
)
from notepatch.platform.storage import StorageService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from notepatch.modules.tasks.services.task import TaskService
from notepatch.modules.learning.services.content_operations import LearningContentOperations
from notepatch.modules.learning.services.grading_operations import LearningGradingOperations


DOCUMENT_ROLE_BY_KIND = {
    "courseware": "courseware",
    "note": "user_note",
    "homework": "homework",
    "corrected_homework": "homework",
    "exam": "exam",
    "answer_key": "answer_key",
    "rubric": "rubric",
}


class LearningWorkflowService(LearningContentOperations, LearningGradingOperations):
    def __init__(
        self,
        db: Session,
        storage: StorageService | None = None,
        *,
        skill_runner: OpenClawSkillRunner | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.skill_runner = skill_runner
        self.embedding_client = embedding_client or EmbeddingClient()

    def ensure_learning_unit_for_document(self, document: Document) -> LearningUnit:
        if document.document_kind == CHAT_ATTACHMENT_KIND:
            raise PermanentTaskError("Chat attachments cannot be added to learning units")
        metadata = dict(document.metadata_ or {})
        learning_unit_id = metadata.get("learning_unit_id")
        if isinstance(learning_unit_id, str) and learning_unit_id:
            learning_unit = self.db.scalar(
                select(LearningUnit).where(
                    LearningUnit.workspace_id == document.workspace_id,
                    LearningUnit.id == learning_unit_id,
                )
            )
            if learning_unit is not None:
                self._link_document(learning_unit, document)
                self.db.commit()
                return learning_unit

        subject = _string_or_none(metadata.get("subject"))
        grade_level = _string_or_none(metadata.get("grade_level"))
        topic = _string_or_none(metadata.get("topic"))
        title = (
            _string_or_none(metadata.get("learning_unit_title"))
            or document.title
            or document.original_filename
            or "学习资料"
        )
        learning_unit = LearningUnit(
            workspace_id=document.workspace_id,
            title=title[:255],
            subject=subject,
            grade_level=grade_level,
            topic=topic,
            metadata_={"source": "automatic_pipeline", "source_document_id": document.id},
        )
        self.db.add(learning_unit)
        self.db.flush()
        metadata["learning_unit_id"] = learning_unit.id
        document.metadata_ = metadata
        self._link_document(learning_unit, document)
        self.db.commit()
        self.db.refresh(learning_unit)
        return learning_unit

    def schedule_after_upload(self, document: Document) -> Task | None:
        if document.document_kind not in AUTO_LEARNING_DOCUMENT_KINDS:
            return None
        if self._existing_task_by_resource(
            document.workspace_id, "document_processing_pipeline", document.id
        ):
            return None
        learning_unit = self.ensure_learning_unit_for_document(document)
        return TaskService(self.db).create_task(
            workspace_id=document.workspace_id,
            task_type="document_processing_pipeline",
            resource_type="document",
            resource_id=document.id,
            payload={
                "document_id": document.id,
                "options": {"auto_learning": True},
                "learning_unit_id": learning_unit.id,
            },
        )

    def schedule_after_ocr(
        self,
        *,
        document: Document,
        ocr_artifacts: dict[str, DocumentArtifact],
        force_reprocess: bool = False,
    ) -> list[Task]:
        if document.document_kind == CHAT_ATTACHMENT_KIND:
            return []
        learning_unit = self.ensure_learning_unit_for_document(document)
        ocr_run_id = (ocr_artifacts.get("ocr_json").metadata_ or {}).get("ocr_run_id")
        common = {
            "document_id": document.id,
            "learning_unit_id": learning_unit.id,
            "source_ocr_run_id": ocr_run_id,
            "force_reprocess": force_reprocess,
        }
        specs: list[tuple[str, str, str, dict]] = []
        if document.document_kind in {"courseware", "note", "other"}:
            specs.append(("build_knowledge_base", "document", document.id, common))
        elif document.document_kind in {"homework", "corrected_homework", "exam"}:
            specs.append(("extract_questions", "document", document.id, common))
        # answer keys and rubrics are OCR sources consumed by grading; they do not trigger notes.
        return self._create_unique_tasks(specs, force_reprocess=force_reprocess)

    def ensure_homework_for_document(
        self,
        document: Document,
        learning_unit: LearningUnit | None = None,
    ) -> Homework:
        existing = self.db.scalar(
            select(Homework).where(
                Homework.workspace_id == document.workspace_id,
                Homework.document_id == document.id,
            )
        )
        if existing is not None:
            return existing
        homework = Homework(
            workspace_id=document.workspace_id,
            title=document.title or document.original_filename,
            description="Created from an uploaded homework document.",
            document_id=document.id,
            status="draft",
            metadata_={
                "source": "automatic_pipeline",
                "learning_unit_id": learning_unit.id if learning_unit else None,
            },
            created_by_user_id=document.uploaded_by,
        )
        self.db.add(homework)
        self.db.commit()
        self.db.refresh(homework)
        return homework

    def _skill(self) -> OpenClawSkillRunner:
        if self.skill_runner is None:
            raise RuntimeError("OpenClaw skill runner is required for learning tasks")
        return self.skill_runner

    def _ensure_active(self, task: Task) -> None:
        TaskService(self.db).ensure_active(task)

    def _record_task_event(self, task: Task, event_type: str, data: dict) -> None:
        messages = {
            "gpu_lease_waiting": "Waiting for shared GPU lease",
            "gpu_lease_acquired": "Shared GPU lease acquired",
            "gpu_lease_released": "Shared GPU lease released",
            "gpu_lease_timeout": "Timed out waiting for shared GPU lease",
        }
        TaskService(self.db).add_event(
            task,
            event_type,
            messages.get(event_type, event_type.replace("_", " ").title()),
            level="error" if event_type.endswith("timeout") else "info",
            data=data,
        )
        self.db.commit()

    def _required_ocr_text(self, document: Document) -> str:
        value = self._latest_ocr_text(document)
        if not value or not value.strip():
            raise PermanentTaskError(f"Document {document.id} has no usable OCR text")
        return value

    def _latest_ocr_text(self, document: Document | None) -> str | None:
        if document is None or self.storage is None:
            return None
        artifact = self.db.scalar(
            select(DocumentArtifact)
            .where(
                DocumentArtifact.workspace_id == document.workspace_id,
                DocumentArtifact.document_id == document.id,
                DocumentArtifact.artifact_type.in_(("ocr_markdown", "ocr_text")),
            )
            .order_by(DocumentArtifact.created_at.desc())
        )
        return self._download_text(self.storage, artifact.bucket, artifact.object_key) if artifact else None

    def _grading_references(self, homework: Homework) -> list[dict]:
        rows = self.db.scalars(
            select(HomeworkReference).where(
                HomeworkReference.workspace_id == homework.workspace_id,
                HomeworkReference.homework_id == homework.id,
            )
        ).all()
        references = []
        for row in rows:
            document = self._document(row.document_id, homework.workspace_id)
            references.append(
                {
                    "reference_type": row.reference_type,
                    "document_id": document.id,
                    "document_kind": document.document_kind,
                    "ocr_text": self._required_ocr_text(document),
                }
            )
        return references

    def _store_document_json_artifact(
        self,
        *,
        task: Task,
        document: Document,
        artifact_type: str,
        filename: str,
        payload: dict,
        metadata: dict,
    ) -> DocumentArtifact:
        self._ensure_active(task)
        artifact_id = str(uuid.uuid4())
        extension = Path(filename).suffix.lstrip(".") or "json"
        key = self.storage.document_artifact_key(
            task.workspace_id, document.id, artifact_id, artifact_type, extension
        )
        self.storage.put_json_artifact(key, payload, bucket=document.bucket)
        file_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._ensure_active(task)
        artifact = DocumentArtifact(
            id=artifact_id,
            workspace_id=task.workspace_id,
            document_id=document.id,
            artifact_type=artifact_type,
            bucket=document.bucket,
            object_key=key,
            mime_type="application/json",
            file_size=file_size,
            metadata_={**metadata, "task_id": task.id},
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact

    def _artifact_for_task(self, task: Task, artifact_type: str) -> DocumentArtifact | None:
        artifacts = self.db.scalars(
            select(DocumentArtifact).where(
                DocumentArtifact.workspace_id == task.workspace_id,
                DocumentArtifact.artifact_type == artifact_type,
            )
        ).all()
        return next((item for item in artifacts if (item.metadata_ or {}).get("task_id") == task.id), None)

    def _grading_for_task(self, task_id: str) -> GradingResult | None:
        rows = self.db.scalars(select(GradingResult)).all()
        return next((item for item in rows if (item.metadata_ or {}).get("task_id") == task_id), None)

    def _create_unique_tasks(
        self,
        specs: list[tuple[str, str, str, dict]],
        *,
        force_reprocess: bool,
    ) -> list[Task]:
        created: list[Task] = []
        service = TaskService(self.db)
        for task_type, resource_type, resource_id, payload in specs:
            workspace_id = self._workspace_for_resource(resource_type, resource_id)
            if not force_reprocess and self._existing_task_by_resource(workspace_id, task_type, resource_id):
                continue
            created.append(
                service.create_task(
                    workspace_id=workspace_id,
                    task_type=task_type,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    payload=payload,
                )
            )
        return created

    def schedule_study_notes(self, unit: LearningUnit, *, reason: str) -> Task:
        settings = get_settings()
        run_at = utcnow() + timedelta(seconds=settings.study_note_debounce_seconds)
        service = TaskService(self.db)
        queued = self.db.scalar(
            select(Task)
            .where(
                Task.workspace_id == unit.workspace_id,
                Task.task_type == "generate_study_notes",
                Task.resource_type == "learning_unit",
                Task.resource_id == unit.id,
                Task.status == "queued",
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )
        payload = {
            "learning_unit_id": unit.id,
            "expected_knowledge_revision": unit.knowledge_revision,
            "reason": reason,
        }
        unit.note_generation_due_at = run_at
        if queued is not None:
            queued.payload = payload
            if not service.schedule_task_at(queued, run_at):
                raise RuntimeError("Could not reschedule study note generation")
            return queued
        task = service.create_delayed_task(
            workspace_id=unit.workspace_id,
            task_type="generate_study_notes",
            run_at=run_at,
            resource_type="learning_unit",
            resource_id=unit.id,
            payload=payload,
        )
        self.db.refresh(unit)
        return task

    def schedule_flashcards(self, unit: LearningUnit, note: StudyNoteVersion, *, reason: str) -> Task:
        existing, expected_attempt_revision = self._flashcard_task_for_revision(unit, note)
        if existing is not None:
            return existing
        return TaskService(self.db).create_task(
            workspace_id=unit.workspace_id,
            task_type="generate_flashcards",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={
                "learning_unit_id": unit.id,
                "study_note_version_id": note.id,
                "expected_attempt_revision": expected_attempt_revision,
                "reason": reason,
            },
        )

    def _existing_task_by_resource(self, workspace_id: str, task_type: str, resource_id: str) -> Task | None:
        return self.db.scalar(
            select(Task)
            .where(
                Task.workspace_id == workspace_id,
                Task.task_type == task_type,
                Task.resource_id == resource_id,
                Task.status.in_(("queued", "running", "succeeded")),
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        )

    def _workspace_for_resource(self, resource_type: str, resource_id: str) -> str:
        if resource_type == "document":
            return self._document(resource_id, None).workspace_id
        if resource_type == "homework":
            homework = self.db.get(Homework, resource_id)
            if homework is None:
                raise PermanentTaskError("Homework not found")
            return homework.workspace_id
        unit = self.db.get(LearningUnit, resource_id)
        if unit is None:
            raise PermanentTaskError("Learning unit not found")
        return unit.workspace_id

    def _link_document(self, unit: LearningUnit, document: Document) -> None:
        link = self.db.scalar(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == document.workspace_id,
                LearningUnitDocument.learning_unit_id == unit.id,
                LearningUnitDocument.document_id == document.id,
            )
        )
        role = DOCUMENT_ROLE_BY_KIND.get(document.document_kind, "source")
        if link is None:
            self.db.add(
                LearningUnitDocument(
                    workspace_id=document.workspace_id,
                    learning_unit_id=unit.id,
                    document_id=document.id,
                    role=role,
                )
            )
        else:
            link.role = role

    def _document(self, document_id: Any, workspace_id: str | None) -> Document:
        if not isinstance(document_id, str) or not document_id:
            raise PermanentTaskError("Document not found")
        query = select(Document).where(Document.id == document_id, Document.status != "deleted")
        if workspace_id is not None:
            query = query.where(Document.workspace_id == workspace_id)
        document = self.db.scalar(query)
        if document is None:
            raise PermanentTaskError("Document not found")
        return document

    def _learning_unit(self, unit_id: Any, workspace_id: str) -> LearningUnit:
        if not isinstance(unit_id, str) or not unit_id:
            raise PermanentTaskError("Learning unit not found")
        unit = self.db.scalar(
            select(LearningUnit).where(LearningUnit.workspace_id == workspace_id, LearningUnit.id == unit_id)
        )
        if unit is None:
            raise PermanentTaskError("Learning unit not found")
        return unit

    def _unit_documents(self, unit_id: str, workspace_id: str) -> list[Document]:
        return list(
            self.db.scalars(
                select(Document)
                .join(LearningUnitDocument, LearningUnitDocument.document_id == Document.id)
                .where(
                    LearningUnitDocument.workspace_id == workspace_id,
                    LearningUnitDocument.learning_unit_id == unit_id,
                    Document.workspace_id == workspace_id,
                    Document.status != "deleted",
                )
                .order_by(Document.created_at.asc())
            ).all()
        )

    def _unit_chunks(self, unit_id: str, workspace_id: str) -> list[KnowledgeChunk]:
        chunks = self.db.scalars(
            select(KnowledgeChunk)
            .outerjoin(Document, Document.id == KnowledgeChunk.document_id)
            .where(KnowledgeChunk.workspace_id == workspace_id)
            .where((KnowledgeChunk.document_id.is_(None)) | (Document.status != "deleted"))
            .order_by(KnowledgeChunk.created_at.asc())
        ).all()
        return [chunk for chunk in chunks if (chunk.metadata_ or {}).get("learning_unit_id") == unit_id]

    def _learning_unit_for_homework(self, homework: Homework) -> LearningUnit | None:
        unit_id = (homework.metadata_ or {}).get("learning_unit_id")
        if isinstance(unit_id, str):
            return self.db.scalar(
                select(LearningUnit).where(
                    LearningUnit.workspace_id == homework.workspace_id,
                    LearningUnit.id == unit_id,
                )
            )
        if homework.document_id:
            link = self.db.scalar(
                select(LearningUnitDocument).where(
                    LearningUnitDocument.workspace_id == homework.workspace_id,
                    LearningUnitDocument.document_id == homework.document_id,
                )
            )
            return self.db.get(LearningUnit, link.learning_unit_id) if link else None
        return None

    @staticmethod
    def _document_payload(document: Document) -> dict:
        return {
            "id": document.id,
            "title": document.title,
            "filename": document.original_filename,
            "document_kind": document.document_kind,
            "mime_type": document.mime_type,
            "metadata": document.metadata_ or {},
        }

    @staticmethod
    def _learning_unit_payload(unit: LearningUnit) -> dict:
        return {
            "id": unit.id,
            "title": unit.title,
            "subject": unit.subject,
            "grade_level": unit.grade_level,
            "topic": unit.topic,
        }

    @staticmethod
    def _chunk_payload(chunk: KnowledgeChunk) -> dict:
        return {
            "id": chunk.id,
            "document_id": chunk.document_id,
            "content": chunk.content,
            "subject": chunk.subject,
            "grade_level": chunk.grade_level,
            "metadata": chunk.metadata_ or {},
        }

    @staticmethod
    def _download_text(storage: StorageService, bucket: str, object_key: str) -> str:
        path = Path(tempfile.gettempdir()) / f"notepatch-learning-{uuid.uuid4()}"
        try:
            storage.download_file(bucket, object_key, path)
            return path.read_text(encoding="utf-8", errors="ignore")
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _put_text(storage: StorageService, object_key: str, content: str, content_type: str) -> None:
        path = Path(tempfile.gettempdir()) / f"notepatch-learning-{uuid.uuid4()}"
        try:
            path.write_text(content, encoding="utf-8")
            storage.put_file(storage.bucket, object_key, path, content_type=content_type)
        finally:
            path.unlink(missing_ok=True)


def _string_or_none(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
