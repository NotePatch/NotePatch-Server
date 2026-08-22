from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select

from notepatch.modules.documents.models.document import DocumentArtifact
from notepatch.modules.identity.models.user import User
from notepatch.modules.identity.models.workspace import Workspace
from notepatch.modules.learning.models.homework import Mistake, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePoint,
    LearningUnit,
    StudyNoteVersion,
)
from notepatch.modules.learning.models.note_workflow import NoteGapSuggestion, NoteSet, StudyNoteCorrection
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    KnowledgeBuildResult,
    QuestionExtractionResult,
    ScholarNotesResult,
)
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.errors import PermanentTaskError
from notepatch.platform.storage import StorageService
from notepatch.modules.learning.services.flashcard_priority import FlashcardPriorityService
from notepatch.modules.learning.services.html_notes import (
    ALLOWED_CLASSES,
    sanitize_note_html,
    validate_knowledge_point_references,
    validate_note_structure,
)
from notepatch.modules.learning.services.knowledge_points import KnowledgePointService
from notepatch.modules.learning.services.note_completion import NoteCompletionEvidenceService
from notepatch.modules.learning.services.note_themes import CURRENT_NOTE_THEME_ID
from notepatch.modules.learning.services.note_ir import (
    bind_note_evidence_refs,
    is_notebook_branding_text,
    render_note_ir,
    validate_note_ir,
)
from notepatch.modules.learning.services.note_markdown import NOTE_MARKDOWN_RENDERER_REVISION
from notepatch.platform.config import get_settings


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
                            "student_user_id": homework.created_by_user_id,
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

    @staticmethod
    def _visual_note_documents(documents: list) -> list:
        eligible = [
            document
            for document in documents
            if document.document_kind == "note"
            and document.file_type == "image"
            and document.status in {"uploaded", "ready"}
        ]
        return eligible[-8:]

    def _note_source_blocks(self, documents: list, storage: StorageService) -> list[dict]:
        blocks: list[dict] = []
        documents = sorted(
            documents,
            key=lambda item: (
                0 if (item.metadata_ or {}).get("note_set_id") else 1,
                str((item.metadata_ or {}).get("note_set_id") or ""),
                int((item.metadata_ or {}).get("note_set_page_index", 0)),
                item.created_at,
            ),
        )
        for document in documents:
            if document.document_kind != "note":
                continue
            artifact = self.db.scalar(
                select(DocumentArtifact)
                .where(
                    DocumentArtifact.workspace_id == document.workspace_id,
                    DocumentArtifact.document_id == document.id,
                    DocumentArtifact.artifact_type == "ocr_json",
                )
                .order_by(DocumentArtifact.created_at.desc())
            )
            payload = None
            if artifact is not None:
                try:
                    payload = storage.get_json_artifact(artifact.object_key, bucket=artifact.bucket)
                except Exception:
                    payload = None
            for page in (payload or {}).get("pages", []):
                for index, block in enumerate(page.get("blocks", [])):
                    text = block.get("text") or block.get("latex") or block.get("markdown") or ""
                    if not str(text).strip():
                        continue
                    raw_id = str(block.get("id") or f"block-{index}")
                    blocks.append({
                        "id": f"{document.id}:{raw_id}",
                        "document_id": document.id,
                        "page_index": int(page.get("page_index", 0)),
                        "bbox": list(block.get("bbox") or [0, 0, 1, 1]),
                        "reading_order": int(block.get("reading_order", index + 1)),
                        "type": str(block.get("type") or "text"),
                        "text": str(text),
                        "confidence": float(block.get("confidence") or 0),
                        "notebook_identity_candidate": is_notebook_branding_text(str(text)),
                    })
            if not any(item["document_id"] == document.id for item in blocks):
                text = self._required_ocr_text(document)
                blocks.append({
                    "id": f"{document.id}:document-text",
                    "document_id": document.id,
                    "page_index": 0,
                    "bbox": [0, 0, 1, 1],
                    "reading_order": 1,
                    "type": "text",
                    "text": text,
                    "confidence": 1.0,
                    "notebook_identity_candidate": is_notebook_branding_text(text),
                })
        return blocks

    def _note_policy(self, task: Task, unit: LearningUnit) -> tuple[str, str, int]:
        workspace = self.db.scalar(select(Workspace).where(Workspace.id == unit.workspace_id))
        owner = self.db.get(User, workspace.owner_user_id) if workspace is not None else None
        content = str(task.payload.get("content_edit_level") or (owner.note_content_edit_level if owner else "conceptual"))
        layout = str(task.payload.get("layout_edit_level") or (owner.note_layout_edit_level if owner else "minor"))
        history_limit = int(task.payload.get("history_limit", owner.note_history_limit if owner else 3))
        if content not in {"verbatim", "spelling", "conceptual", "rewrite"}:
            raise PermanentTaskError("Invalid note content edit level")
        if layout not in {"preserve", "minor", "reorder", "reflow"}:
            raise PermanentTaskError("Invalid note layout edit level")
        return content, layout, history_limit

    def build_knowledge_base(self, task: Task, storage: StorageService) -> dict:
        document = self._document(task.payload.get("document_id") or task.resource_id, task.workspace_id)
        learning_unit = self.ensure_learning_unit_for_document(document)
        existing = self.db.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.workspace_id == task.workspace_id,
                KnowledgeChunk.metadata_["task_id"].as_string() == task.id,
            )
        ).all()
        if existing:
            downstream = self.schedule_after_knowledge(
                document, learning_unit, reason="knowledge_reused"
            )
            return {
                "chunks_created": 0,
                "chunk_ids": [chunk.id for chunk in existing],
                "reused": True,
                "downstream_tasks": (
                    [{"id": downstream.id, "task_type": downstream.task_type}]
                    if downstream is not None else []
                ),
            }
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
        points = KnowledgePointService(
            self.db,
            self.embedding_client,
            match_threshold=get_settings().knowledge_point_match_threshold,
        ).resolve_many(
            unit=learning_unit,
            names=[item.title for item in result.chunks],
            source_document_ids=[document.id],
            vectors={item.title: vector for item, vector in zip(result.chunks, vectors, strict=True)},
            owner=f"task:{task.id}:knowledge-points",
        )
        if bool(task.payload.get("force_reprocess")):
            self.db.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.workspace_id == task.workspace_id,
                    KnowledgeChunk.document_id == document.id,
                )
            )
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
                    "knowledge_point_id": points[item.title].id,
                },
            )
            self.db.add(chunk)
            self.db.flush()
            chunk_ids.append(chunk.id)
        learning_unit.knowledge_revision += 1
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        downstream = self.schedule_after_knowledge(
            document, learning_unit, reason="knowledge_updated"
        )
        return {
            "chunks_created": len(chunk_ids),
            "chunk_ids": chunk_ids,
            "output_key": run["output_key"],
            "learning_unit_id": learning_unit.id,
            "knowledge_revision": learning_unit.knowledge_revision,
            "downstream_tasks": (
                [{"id": downstream.id, "task_type": downstream.task_type}]
                if downstream is not None else []
            ),
        }

    def generate_study_notes(self, task: Task, storage: StorageService) -> dict:
        existing = self.db.scalar(select(StudyNoteVersion).where(StudyNoteVersion.task_id == task.id))
        if existing is not None:
            return {"learning_unit_id": existing.learning_unit_id, "study_note_version_id": existing.id, "reused": True}
        unit = self._learning_unit(task.payload.get("learning_unit_id") or task.resource_id, task.workspace_id)
        content_edit_level, layout_edit_level, history_limit = self._note_policy(task, unit)
        expected_revision = int(task.payload.get("expected_knowledge_revision", unit.knowledge_revision))
        expected_attempt_revision = int(
            task.payload.get("expected_attempt_revision", unit.attempt_revision)
        )
        if expected_revision != unit.knowledge_revision or (
            content_edit_level == "rewrite"
            and expected_attempt_revision != unit.attempt_revision
        ):
            replacement = self.schedule_study_notes(
                unit, reason="note_sources_changed_before_note_start",
                content_edit_level=content_edit_level,
                layout_edit_level=layout_edit_level,
                history_limit=history_limit,
            )
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "note_source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        documents = self._unit_documents(unit.id, task.workspace_id)
        note_documents = [document for document in documents if document.document_kind == "note"]
        if not note_documents and not task.payload.get("gap_ids"):
            raise PermanentTaskError("Cannot generate study notes before a note document exists")
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        if not chunks:
            raise PermanentTaskError("Cannot generate study notes before knowledge chunks exist")
        source_blocks = self._note_source_blocks(note_documents, storage) if note_documents else list(task.payload.get("source_blocks") or [])
        note_document_ids = {document.id for document in note_documents}
        current_point_ids = {
            str((chunk.metadata_ or {}).get("knowledge_point_id"))
            for chunk in chunks
            if chunk.document_id in note_document_ids
            and (chunk.metadata_ or {}).get("knowledge_point_id")
        }
        selected_gap_ids = list(dict.fromkeys(task.payload.get("gap_ids") or []))
        if selected_gap_ids:
            selected_gap_point_ids = set(
                self.db.scalars(
                    select(NoteGapSuggestion.knowledge_point_id).where(
                        NoteGapSuggestion.workspace_id == task.workspace_id,
                        NoteGapSuggestion.learning_unit_id == unit.id,
                        NoteGapSuggestion.id.in_(selected_gap_ids),
                        NoteGapSuggestion.status.in_(("pending", "draft", "no_base_note")),
                    )
                ).all()
            )
            if len(selected_gap_point_ids) != len(selected_gap_ids):
                raise PermanentTaskError("One or more selected note gaps are no longer actionable")
            current_point_ids.update(selected_gap_point_ids)
        completion_evidence: list[dict] = []
        completion_evidence_by_id: dict[str, dict] = {}
        completion_source_document_ids: list[str] = []
        completion_evidence_revision: str | None = None
        if content_edit_level == "rewrite":
            completion = NoteCompletionEvidenceService(
                self.db,
                storage,
                self.embedding_client,
            ).select(
                unit=unit,
                documents=documents,
                note_documents=note_documents,
                source_blocks=source_blocks,
                chunks=chunks,
                owner=f"task:{task.id}:note-completion",
                event_callback=lambda event, data: self._record_task_event(task, event, data),
            )
            completion_evidence = completion.evidence
            completion_evidence_by_id = completion.by_id
            completion_source_document_ids = completion.source_document_ids
            completion_evidence_revision = completion.evidence_revision
            current_point_ids.update(completion.knowledge_point_ids)
            TaskService(self.db).add_event(
                task,
                "note_completion_evidence_selected",
                "Selected related sources for rewrite-mode note completion",
                data={
                    "evidence_count": len(completion_evidence),
                    "authoritative_count": sum(
                        1 for item in completion_evidence if item.get("authoritative")
                    ),
                    "source_document_ids": completion_source_document_ids,
                    "similarity_threshold": get_settings().note_rewrite_completion_similarity_threshold,
                    "evidence_revision": completion_evidence_revision,
                },
            )
            self.db.commit()
        points = (
            self.db.scalars(
                select(KnowledgePoint).where(
                    KnowledgePoint.workspace_id == task.workspace_id,
                    KnowledgePoint.learning_unit_id == unit.id,
                    KnowledgePoint.id.in_(current_point_ids),
                )
            ).all()
            if current_point_ids
            else []
        )
        allowed_point_ids = {point.id for point in points}

        def validate_scholar_output(candidate: ScholarNotesResult) -> None:
            result_point_ids = {item.id for item in candidate.knowledge_points}
            if not result_point_ids.issubset(allowed_point_ids):
                raise ValueError("Scholar notes returned unknown knowledge point ids")
            validate_note_ir(
                candidate,
                source_blocks,
                content_edit_level=content_edit_level,
                layout_edit_level=layout_edit_level,
                completion_evidence=completion_evidence_by_id,
            )

        image_note_documents = self._visual_note_documents(note_documents)
        visual_document_ids = [document.id for document in image_note_documents]
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_scholar_notes",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "documents": [self._document_payload(document) for document in note_documents],
                "source_blocks": source_blocks,
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
                "completion_evidence": completion_evidence,
                "note_policy": {
                    "content_edit_level": content_edit_level,
                    "layout_edit_level": layout_edit_level,
                    "content_rules": {
                        "verbatim": "Only correct OCR transcription errors by comparing the source image.",
                        "spelling": "Only correct OCR and source spelling errors.",
                        "conceptual": "Correct OCR, spelling, and high-confidence serious concept errors without changing voice.",
                        "rewrite": (
                            "Rebuild the entire manuscript as a coherent study note rather than copying OCR blocks. "
                            "Merge related fragments and use same-document knowledge chunks to explain facts already "
                            "present. Evidence-backed supplements may add only directly related missing content; do not "
                            "import the whole unit or invent unsupported facts."
                        ),
                    }[content_edit_level],
                    "layout_rules": {
                        "preserve": "Keep source block order, grouping, and relative layout.",
                        "minor": "Keep block order; only repair obvious alignment and marginal annotation placement.",
                        "reorder": "Blocks may move vertically but no source content may be removed.",
                        "reflow": "A new layout may be designed.",
                    }[layout_edit_level],
                    "notebook_identity_policy": (
                        "preserve_all_source_text"
                        if content_edit_level == "verbatim"
                        else "exclude_school_company_manufacturer_publisher_and_other_notebook_branding"
                    ),
                },
                "knowledge_points": [
                    {"id": point.id, "name": point.name, "source_document_ids": point.source_document_ids}
                    for point in points
                ],
                "visual_layout_policy": {
                    "content_authority": "note_source_blocks",
                    "reference_authority": (
                        "selected_completion_evidence_may_support_supplements"
                        if content_edit_level == "rewrite"
                        else "knowledge_chunks_may_only_support_declared_concept_corrections"
                    ),
                    "image_role": "transcription_and_layout_reference_only",
                    "embed_source_images": False,
                    "layout_edit_level": layout_edit_level,
                },
            },
            output_filename="study_note.json",
            schema=ScholarNotesResult,
            visual_document_ids=visual_document_ids,
            output_validator=validate_scholar_output,
        )
        self._ensure_active(task)
        self.db.refresh(unit)
        if unit.knowledge_revision != expected_revision or (
            content_edit_level == "rewrite"
            and unit.attempt_revision != expected_attempt_revision
        ):
            replacement = self.schedule_study_notes(
                unit, reason="note_sources_changed_during_note_generation",
                content_edit_level=content_edit_level,
                layout_edit_level=layout_edit_level,
                history_limit=history_limit,
            )
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "note_source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        result_point_ids = {item.id for item in result.knowledge_points}
        if not result_point_ids.issubset(allowed_point_ids):
            raise PermanentTaskError("Scholar notes returned unknown knowledge point ids")
        version_id = str(uuid.uuid4())
        try:
            validate_note_ir(
                result, source_blocks,
                content_edit_level=content_edit_level,
                layout_edit_level=layout_edit_level,
                completion_evidence=completion_evidence_by_id,
            )
            bind_note_evidence_refs(result, completion_evidence_by_id)
            html = render_note_ir(result)
            validate_note_structure(html)
            validate_knowledge_point_references(html, allowed_point_ids)
        except ValueError as exc:
            raise PermanentTaskError(str(exc)) from exc
        version_no = int(
            self.db.scalar(
                select(func.coalesce(func.max(StudyNoteVersion.version_no), 0)).where(
                    StudyNoteVersion.workspace_id == task.workspace_id,
                    StudyNoteVersion.learning_unit_id == unit.id,
                )
            )
            or 0
        ) + 1
        html_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "html")
        json_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "study_note", "json")
        ir_key = StorageService.learning_unit_note_key(task.workspace_id, unit.id, version_id, "note_ir", "json")
        source_document_ids = list(dict.fromkeys([
            *(
                str(block.get("document_id"))
                for block in source_blocks
                if block.get("document_id")
            ),
            *completion_source_document_ids,
        ]))
        completion_blocks = [
            block for block in result.note_ir.blocks if block.origin == "evidence_supplement"
        ]
        structured = result.model_dump(mode="json")
        structured["source_document_ids"] = source_document_ids
        structured["html"] = html
        structured["policy"] = {"content_edit_level": content_edit_level, "layout_edit_level": layout_edit_level}
        self._put_text(storage, html_key, html, "text/html; charset=utf-8")
        storage.put_json_artifact(json_key, structured, bucket=storage.bucket)
        storage.put_json_artifact(ir_key, result.note_ir.model_dump(mode="json"), bucket=storage.bucket)
        self._ensure_active(task)
        note = StudyNoteVersion(
            id=version_id,
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            task_id=task.id,
            version_no=version_no,
            title=result.title,
            html_object_key=html_key,
            json_object_key=json_key,
            note_ir_object_key=ir_key,
            content_edit_level=content_edit_level,
            layout_edit_level=layout_edit_level,
            knowledge_point_ids=[item.id for item in result.knowledge_points],
            source_document_ids=source_document_ids,
            source_mistake_ids=[],
            edit_origin="skill",
            metadata_={
                "skill": "notepatch_scholar_notes",
                "skill_output_key": run["output_key"],
                "theme_id": CURRENT_NOTE_THEME_ID,
                "renderer_revision": NOTE_MARKDOWN_RENDERER_REVISION,
                "visual_reference_document_ids": run.get("visual_reference", {}).get("document_ids", []),
                "visual_reference_mode": run.get("visual_reference", {}).get("mode", "none"),
                "visual_reference_selection_policy": run.get("visual_reference", {}).get(
                    "selection_policy", "latest_8_image_notes"
                ),
                "content_edit_level": content_edit_level,
                "layout_edit_level": layout_edit_level,
                "history_limit": history_limit,
                "completion_count": len(completion_blocks),
                "completion_source_document_ids": completion_source_document_ids,
                "completion_evidence_revision": completion_evidence_revision,
                "completion_strategy": (
                    "rewrite_related_evidence_v1" if content_edit_level == "rewrite" else None
                ),
                "source_images_embedded": False,
                "excluded_notebook_identity_blocks": [
                    item.model_dump(mode="json") for item in result.excluded_source_blocks
                ],
                "legacy": False,
            },
        )
        self.db.add(note)
        self.db.flush()
        for correction in result.corrections:
            self.db.add(StudyNoteCorrection(
                workspace_id=task.workspace_id, learning_unit_id=unit.id, note_version_id=note.id,
                source_block_id=correction.source_block_id, correction_type=correction.correction_type,
                original_text=correction.original_text, corrected_text=correction.corrected_text,
                reason=correction.reason, confidence=correction.confidence, source_refs=correction.source_refs,
            ))
        if selected_gap_ids:
            for suggestion in self.db.scalars(
                select(NoteGapSuggestion).where(
                    NoteGapSuggestion.workspace_id == task.workspace_id,
                    NoteGapSuggestion.learning_unit_id == unit.id,
                    NoteGapSuggestion.id.in_(selected_gap_ids),
                )
            ).all():
                suggestion.status = "accepted"
                suggestion.accepted_version_id = note.id
        unit.notes_generated_revision = expected_revision
        unit.note_generation_due_at = None
        for note_set in self.db.scalars(
            select(NoteSet).where(
                NoteSet.workspace_id == task.workspace_id,
                NoteSet.learning_unit_id == unit.id,
                NoteSet.status == "processing",
            )
        ).all():
            note_set.status = "ready"
        self._ensure_active(task)
        self.db.commit()
        self._ensure_active(task)
        flashcard_task = self.schedule_flashcards(unit, note, reason="study_note_generated")
        retention_task = TaskService(self.db).create_task(
            workspace_id=unit.workspace_id, task_type="purge_study_note_history",
            resource_type="learning_unit", resource_id=unit.id,
            payload={"learning_unit_id": unit.id, "keep_history": history_limit},
        )
        downstream_tasks = [flashcard_task, retention_task]
        mistakes = self.db.scalars(
            select(Mistake).where(
                Mistake.workspace_id == task.workspace_id,
                Mistake.status == "open",
            )
        ).all()
        mistake_ids = [
            mistake.id
            for mistake in mistakes
            if (mistake.metadata_ or {}).get("learning_unit_id") == unit.id
        ]
        if mistake_ids:
            downstream_tasks.extend(
                self._create_unique_tasks(
                    [
                        (
                            "highlight_study_notes",
                            "learning_unit",
                            unit.id,
                            {
                                "learning_unit_id": unit.id,
                                "mistake_ids": mistake_ids,
                                "expected_note_version_id": note.id,
                                "reason": "study_note_generated",
                            },
                        )
                    ],
                    force_reprocess=True,
                )
            )
        return {
            "learning_unit_id": unit.id,
            "study_note_version_id": note.id,
            "html_key": html_key,
            "json_key": json_key,
            "downstream_tasks": [
                {"id": downstream.id, "task_type": downstream.task_type}
                for downstream in downstream_tasks
            ],
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
        if note is None:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_study_note"}
        expected_note_id = task.payload.get("study_note_version_id") or note.id
        expected_attempt_revision = int(task.payload.get("expected_attempt_revision", unit.attempt_revision))
        existing_deck = self._flashcard_deck_for_revision(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=expected_note_id,
            attempt_revision=expected_attempt_revision,
        )
        if existing_deck is not None:
            return {"learning_unit_id": unit.id, "flashcard_deck_id": existing_deck.id, "reused": True}
        if note.id != expected_note_id or unit.attempt_revision != expected_attempt_revision:
            replacement = self.schedule_flashcards(unit, note, reason="flashcard_source_changed")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        chunks = self._unit_chunks(unit.id, task.workspace_id)
        note_html = self._download_text(storage, storage.bucket, note.html_object_key)
        priority_service = FlashcardPriorityService(self.db)
        weighted_points = priority_service.calculate(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            note=note,
        )
        if not weighted_points:
            return {"learning_unit_id": unit.id, "skipped": True, "reason": "no_knowledge_points"}
        result, run = self._skill().execute(
            task=task,
            skill_name="notepatch_flashcards",
            input_payload={
                "learning_unit": self._learning_unit_payload(unit),
                "study_note_html": note_html,
                "weighted_knowledge_points": weighted_points,
                "knowledge_chunks": [self._chunk_payload(chunk) for chunk in chunks],
            },
            output_filename="flashcards.json",
            schema=FlashcardsSkillResult,
        )
        self._ensure_active(task)
        self.db.refresh(unit)
        latest_note = self.db.scalar(
            select(StudyNoteVersion)
            .where(StudyNoteVersion.workspace_id == task.workspace_id, StudyNoteVersion.learning_unit_id == unit.id)
            .order_by(StudyNoteVersion.version_no.desc())
        )
        if latest_note is None or latest_note.id != expected_note_id or unit.attempt_revision != expected_attempt_revision:
            replacement = self.schedule_flashcards(unit, latest_note or note, reason="flashcard_source_changed_during_run")
            return {
                "learning_unit_id": unit.id,
                "skipped": True,
                "reason": "source_revision_changed",
                "replacement_task_id": replacement.id,
            }
        candidates = {item["id"]: item for item in weighted_points}
        returned_ids = [item.knowledge_point_id for item in result.flashcards]
        unknown_ids = sorted(set(returned_ids) - set(candidates))
        if unknown_ids:
            raise PermanentTaskError(f"Flashcards contain unknown knowledge point ids: {unknown_ids}")
        fingerprints = {
            (item.front.strip().casefold(), item.back.strip().casefold())
            for item in result.flashcards
        }
        if len(fingerprints) != len(result.flashcards):
            raise PermanentTaskError("Flashcards contain duplicate card content")
        locked_unit = self.db.scalar(select(LearningUnit).where(LearningUnit.id == unit.id).with_for_update())
        existing_deck = self._flashcard_deck_for_revision(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=expected_note_id,
            attempt_revision=expected_attempt_revision,
        )
        if existing_deck is not None:
            del locked_unit
            self.db.commit()
            return {"learning_unit_id": unit.id, "flashcard_deck_id": existing_deck.id, "reused": True}
        version_no = int(
            self.db.scalar(
                select(func.coalesce(func.max(FlashcardDeck.version_no), 0)).where(
                    FlashcardDeck.workspace_id == task.workspace_id,
                    FlashcardDeck.learning_unit_id == unit.id,
                )
            )
            or 0
        ) + 1
        deck = FlashcardDeck(
            workspace_id=task.workspace_id,
            learning_unit_id=unit.id,
            study_note_version_id=note.id,
            task_id=task.id,
            version_no=version_no,
            attempt_revision=expected_attempt_revision,
            weighting_config=priority_service.weighting_config(),
            metadata_={"skill": "notepatch_flashcards", "skill_output_key": run["output_key"]},
        )
        self.db.add(deck)
        self.db.flush()
        for rank, item in enumerate(result.flashcards, start=1):
            candidate = candidates[item.knowledge_point_id]
            self.db.add(
                Flashcard(
                    workspace_id=task.workspace_id,
                    deck_id=deck.id,
                    knowledge_point_id=item.knowledge_point_id,
                    front=item.front,
                    back=item.back,
                    priority_score=candidate["priority_score"],
                    priority_factors=candidate["priority_factors"],
                    source_refs=item.source_refs,
                    difficulty=item.difficulty,
                    rank=rank,
                )
            )
        del locked_unit
        self.db.commit()
        return {
            "learning_unit_id": unit.id,
            "flashcard_deck_id": deck.id,
            "version_no": deck.version_no,
            "cards_created": len(result.flashcards),
            **run,
        }

    def _flashcard_deck_for_revision(
        self,
        *,
        workspace_id: str,
        learning_unit_id: str,
        study_note_version_id: str,
        attempt_revision: int,
    ) -> FlashcardDeck | None:
        return self.db.scalar(
            select(FlashcardDeck).where(
                FlashcardDeck.workspace_id == workspace_id,
                FlashcardDeck.learning_unit_id == learning_unit_id,
                FlashcardDeck.study_note_version_id == study_note_version_id,
                FlashcardDeck.attempt_revision == attempt_revision,
            )
        )

    def _flashcard_task_for_revision(
        self,
        unit: LearningUnit,
        note: StudyNoteVersion,
    ) -> tuple[Task | None, int]:
        locked_unit = self.db.scalar(
            select(LearningUnit).where(
                LearningUnit.workspace_id == unit.workspace_id,
                LearningUnit.id == unit.id,
            ).with_for_update()
        )
        if locked_unit is None:
            raise PermanentTaskError("Learning unit not found")
        expected_attempt_revision = locked_unit.attempt_revision
        candidates = self.db.scalars(
            select(Task)
            .where(
                Task.workspace_id == unit.workspace_id,
                Task.task_type == "generate_flashcards",
                Task.resource_type == "learning_unit",
                Task.resource_id == unit.id,
                Task.status.in_(("queued", "running", "succeeded")),
                Task.cancel_requested_at.is_(None),
            )
            .order_by(Task.created_at.desc())
        ).all()
        existing = next(
            (
                task
                for task in candidates
                if (task.payload or {}).get("study_note_version_id") == note.id
                and int((task.payload or {}).get("expected_attempt_revision", -1))
                == expected_attempt_revision
            ),
            None,
        )
        if existing is not None:
            self.db.commit()
        return existing, expected_attempt_revision
