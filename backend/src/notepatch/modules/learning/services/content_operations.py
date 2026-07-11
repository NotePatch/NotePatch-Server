from __future__ import annotations

import uuid

from sqlalchemy import func, select

from notepatch.modules.learning.models.homework import Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    KnowledgeBuildResult,
    QuestionExtractionResult,
    ScholarNotesResult,
)
from notepatch.modules.tasks.models.task import Task
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService


class LearningContentOperations:
    def extract_questions(self, task: Task, storage: StorageService) -> dict:
        existing = self._artifact_for_task(task, "questions_json")
        if existing is not None:
            return {"document_id": existing.document_id, "artifact_id": existing.id, "output_key": existing.object_key}
        document = self._document(task.payload.get("document_id") or task.resource_id, task.workspace_id)
        source_text = self._required_ocr_text(document)
        learning_unit = self.ensure_learning_unit_for_document(document)
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_question_extractor",
            input_payload={
                "document": self._document_payload(document),
                "learning_unit": self._learning_unit_payload(learning_unit),
                "ocr_text": source_text,
            },
            output_filename="questions.json",
            schema=QuestionExtractionResult,
        )
        self._ensure_active(task)
        artifact = self._store_document_json_artifact(
            task=task,
            document=document,
            artifact_type="questions_json",
            filename="questions.json",
            payload=result.model_dump(mode="json"),
            metadata={"skill": "notepatch_question_extractor", "source_ocr_run_id": task.payload.get("source_ocr_run_id")},
        )
        homework = None
        if document.document_kind in {"homework", "corrected_homework"}:
            homework = self.ensure_homework_for_document(document, learning_unit)
        for item in result.questions:
            self._ensure_active(task)
            self.db.add(
                Question(
                    workspace_id=task.workspace_id,
                    document_id=document.id,
                    homework_id=homework.id if homework else None,
                    sequence_no=item.sequence_no,
                    question_type=item.question_type,
                    prompt=item.prompt,
                    answer=item.answer,
                    metadata_={
                        "skill": "notepatch_question_extractor",
                        "task_id": task.id,
                        "page_refs": item.page_refs,
                        "evidence": item.evidence,
                    },
                )
            )
        self._ensure_active(task)
        self.db.commit()
        downstream: list[Task] = []
        if homework is not None:
            self._ensure_active(task)
            downstream = self._create_unique_tasks(
                [
                    (
                        "grade_homework",
                        "homework",
                        homework.id,
                        {
                            "homework_id": homework.id,
                            "document_id": document.id,
                            "learning_unit_id": learning_unit.id,
                            "source_question_task_id": task.id,
                        },
                    )
                ],
                force_reprocess=bool(task.payload.get("force_reprocess")),
            )
        return {
            "document_id": document.id,
            "artifact_id": artifact.id,
            "questions_created": len(result.questions),
            "output_key": artifact.object_key,
            "skill_output_key": run["output_key"],
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in downstream],
        }

    def build_knowledge_base(self, task: Task, storage: StorageService) -> dict:
        existing = self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.workspace_id == task.workspace_id,
                KnowledgeChunk.metadata_["task_id"].as_string() == task.id,
            )
        ).all()
        if existing:
            return {"chunks_created": 0, "chunk_ids": [chunk.id for chunk in existing], "reused": True}
        document = self._document(task.payload.get("document_id") or task.resource_id, task.workspace_id)
        learning_unit = self.ensure_learning_unit_for_document(document)
        source_text = self._required_ocr_text(document)
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_kb_builder",
            input_payload={
                "document": self._document_payload(document),
                "learning_unit": self._learning_unit_payload(learning_unit),
                "ocr_text": source_text,
            },
            output_filename="knowledge_chunks.json",
            schema=KnowledgeBuildResult,
        )
        self._ensure_active(task)
        vectors = self.embedding_client.embed(
            [chunk.content for chunk in result.chunks],
            owner=f"task:{task.id}:knowledge",
            event_callback=lambda event, data: self._record_task_event(task, event, data),
        )
        self._ensure_active(task)
        chunk_ids: list[str] = []
        for item, vector in zip(result.chunks, vectors, strict=True):
            chunk = KnowledgeChunk(
                workspace_id=task.workspace_id,
                document_id=document.id,
                subject=item.subject or learning_unit.subject,
                grade_level=item.grade_level or learning_unit.grade_level,
                source_type="openclaw_skill",
                content=item.content,
                embedding=vector,
                metadata_={
                    "skill": "notepatch_kb_builder",
                    "task_id": task.id,
                    "learning_unit_id": learning_unit.id,
                    "title": item.title,
                    "key_terms": item.key_terms,
                    "page_refs": item.page_refs,
                    "difficulty": item.difficulty,
                    "prerequisites": item.prerequisites,
                    "order": item.order,
                    "source_ocr_run_id": task.payload.get("source_ocr_run_id"),
                },
            )
            self.db.add(chunk)
            self.db.flush()
            chunk_ids.append(chunk.id)
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        notes = self._create_unique_tasks(
            [
                (
                    "generate_study_notes",
                    "learning_unit",
                    learning_unit.id,
                    {"learning_unit_id": learning_unit.id, "source_knowledge_task_id": task.id},
                )
            ],
            force_reprocess=bool(task.payload.get("force_reprocess")),
        )
        return {
            "chunks_created": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "output_key": run["output_key"],
            "learning_unit_id": learning_unit.id,
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in notes],
        }

    def generate_study_notes(self, task: Task, storage: StorageService) -> dict:
        existing = self.db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.task_id == task.id))
        if existing is not None:
            return {"learning_unit_id": existing.learning_unit_id, "study_note_version_id": existing.id, "reused": True}
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        documents = self._unit_documents(unit.id, task.workspace_id)
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        if not chunks:
            raise PermanentTaskError("Cannot generate study notes before knowledge chunks exist")
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_scholar_notes",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "documents": [self._document_payload(document) for document in documents],
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
            },
            output_filename="study_note.json",
            schema=ScholarNotesResult,
        )
        self._ensure_active(task)
        version_id = str(uuid.uuid4())
        version_no = int(
            self.db.scalar(
                select(func.coalesce(func.max(StudyNoteVersion.version_no), 0)).where(
                    StudyNoteVersion.workspace_id == task.workspace_id,
                    StudyNoteVersion.learning_unit_id == unit.id,
                )
            )
            or 0
        ) + 1
        md_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "md")
        json_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "json")
        self._put_text(storage, md_key, result.markdown, "text/markdown; charset=utf-8")
        storage.put_json_artifact(json_key, result.model_dump(mode="json"), bucket=storage.bucket)
        self._ensure_active(task)
        note = StudyNoteVersion(
            id=version_id,
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            task_id=task.id,
            version_no=version_no,
            title=result.title,
            markdown_object_key=md_key,
            json_object_key=json_key,
            source_document_ids=result.source_document_ids or [document.id for document in documents],
            source_mistake_ids=[],
            metadata_={"skill": "notepatch_scholar_notes", "skill_output_key": run["output_key"]},
        )
        self.db.add(note)
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        flashcards = self._create_unique_tasks(
            [
                (
                    "generate_flashcards",
                    "learning_unit",
                    unit.id,
                    {"learning_unit_id": unit.id, "study_note_version_id": note.id},
                )
            ],
            force_reprocess=True,
        )
        return {
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id,
            "markdown_key": md_key,
            "json_key": json_key,
            "downstream_tasks": [{"id": item.id, "task_type": item.task_type} for item in flashcards],
        }

    def generate_flashcards(self, task: Task, storage: StorageService) -> dict:
        unit_id = task.payload.get("learning_unit_id") or task.resource_id
        if not isinstance(unit_id, str):
            document = self._document(task.payload.get("document_id"), task.workspace_id)
            unit_id = self.ensure_learning_unit_for_document(document).id
        unit = self._learning_unit(unit_id, task.workspace_id)
        note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        note_text = self._download_text(storage, storage.bucket, note.markdown_object_key) if note else None
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_flashcards",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "study_note_markdown": note_text,
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
            },
            output_filename="flashcards.json",
            schema=FlashcardsSkillResult,
        )
        self._ensure_active(task)
        return {"learning_unit_id": unit.id, "flashcards": result.model_dump(mode="json")["flashcards"], **run}
