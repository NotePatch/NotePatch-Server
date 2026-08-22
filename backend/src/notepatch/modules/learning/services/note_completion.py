from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import GradingResult, Homework, Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import KnowledgePointAttempt, LearningUnit
from notepatch.modules.learning.services.embedding import EmbeddingClient
from notepatch.modules.learning.services.knowledge_points import cosine_similarity
from notepatch.modules.learning.services.note_ir import is_notebook_branding_text
from notepatch.platform.config import get_settings
from notepatch.platform.storage import StorageService


SOURCE_PRIORITY = {
    "answer_key_ocr": 60,
    "rubric_ocr": 60,
    "courseware_ocr": 50,
    "knowledge_chunk": 50,
    "homework_question": 20,
    "grading_feedback": 10,
}


@dataclass(slots=True)
class NoteCompletionSelection:
    evidence: list[dict]
    source_document_ids: list[str]
    knowledge_point_ids: list[str]
    evidence_revision: str

    @property
    def by_id(self) -> dict[str, dict]:
        return {item["id"]: item for item in self.evidence}


class NoteCompletionEvidenceService:
    """Select trusted, topic-relevant sources for rewrite-mode note completion."""

    def __init__(
        self,
        db: Session,
        storage: StorageService,
        embedding_client: EmbeddingClient,
    ) -> None:
        self.db = db
        self.storage = storage
        self.embedding_client = embedding_client
        self.settings = get_settings()

    def select(
        self,
        *,
        unit: LearningUnit,
        documents: list[Document],
        note_documents: list[Document],
        source_blocks: list[dict],
        chunks: list[KnowledgeChunk],
        owner: str,
        event_callback: Callable[[str, dict], None] | None = None,
    ) -> NoteCompletionSelection:
        note_document_ids = {item.id for item in note_documents}
        documents_by_id = {item.id: item for item in documents}
        candidates = self._knowledge_candidates(chunks, note_document_ids, documents_by_id)
        candidates.extend(
            self._ocr_candidates(documents, note_document_ids, chunks)
        )
        candidates.extend(
            self._homework_signals(unit, documents, note_document_ids)
        )
        candidates = [
            item
            for item in candidates
            if item.get("text") and not is_notebook_branding_text(str(item["text"]))
        ]
        if not candidates:
            return self._selection(unit, [])

        query = self._query_text(unit, source_blocks)
        callback = event_callback or (lambda _event, _data: None)
        query_vector = self.embedding_client.embed(
            [query], owner=f"{owner}:query", event_callback=callback
        )[0]
        missing = [item for item in candidates if item.get("embedding") is None]
        if missing:
            vectors = self.embedding_client.embed(
                [item["text"] for item in missing],
                owner=f"{owner}:evidence",
                event_callback=callback,
            )
            for item, vector in zip(missing, vectors, strict=True):
                item["embedding"] = vector

        threshold = self.settings.note_rewrite_completion_similarity_threshold
        for item in candidates:
            item["relevance_score"] = cosine_similarity(query_vector, item["embedding"])
        eligible = [item for item in candidates if item["relevance_score"] >= threshold]
        authoritative = sorted(
            (item for item in eligible if item["authoritative"]),
            key=self._rank_key,
            reverse=True,
        )
        signals = sorted(
            (item for item in eligible if not item["authoritative"]),
            key=self._rank_key,
            reverse=True,
        )
        limit = max(1, self.settings.note_rewrite_completion_max_evidence)
        signal_limit = min(4, max(1, limit // 3))
        selected = authoritative[:limit]
        selected.extend(signals[: min(signal_limit, max(0, limit - len(selected)))])
        selected = sorted(selected, key=self._rank_key, reverse=True)[:limit]
        for item in selected:
            item.pop("embedding", None)
            item["text"] = item["text"][: self.settings.note_rewrite_completion_evidence_max_chars]
            item["excerpt"] = item["text"][:1000]
        return self._selection(unit, selected)

    def _knowledge_candidates(
        self,
        chunks: list[KnowledgeChunk],
        note_document_ids: set[str],
        documents_by_id: dict[str, Document],
    ) -> list[dict]:
        items = []
        for chunk in chunks:
            if chunk.document_id in note_document_ids:
                continue
            document = documents_by_id.get(chunk.document_id) if chunk.document_id else None
            items.append(
                {
                    "id": f"chunk:{chunk.id}",
                    "source_type": "knowledge_chunk",
                    "authoritative": True,
                    "document_id": chunk.document_id,
                    "document_kind": document.document_kind if document else None,
                    "knowledge_chunk_id": chunk.id,
                    "question_id": None,
                    "grading_result_id": None,
                    "knowledge_point_id": (chunk.metadata_ or {}).get("knowledge_point_id"),
                    "page_index": self._first_page((chunk.metadata_ or {}).get("page_refs")),
                    "block_id": None,
                    "text": chunk.content.strip(),
                    "embedding": list(chunk.embedding) if chunk.embedding is not None else None,
                }
            )
        return [item for item in items if item["text"]]

    def _ocr_candidates(
        self,
        documents: list[Document],
        note_document_ids: set[str],
        chunks: list[KnowledgeChunk],
    ) -> list[dict]:
        chunk_document_ids = {item.document_id for item in chunks if item.document_id}
        source_types = {
            "courseware": "courseware_ocr",
            "answer_key": "answer_key_ocr",
            "rubric": "rubric_ocr",
        }
        items = []
        for document in documents:
            if document.id in note_document_ids or document.document_kind not in source_types:
                continue
            if document.document_kind == "courseware" and document.id in chunk_document_ids:
                continue
            text = self._latest_ocr_text(document)
            if not text:
                continue
            items.append(
                {
                    "id": f"ocr:{document.id}",
                    "source_type": source_types[document.document_kind],
                    "authoritative": True,
                    "document_id": document.id,
                    "document_kind": document.document_kind,
                    "knowledge_chunk_id": None,
                    "question_id": None,
                    "grading_result_id": None,
                    "knowledge_point_id": None,
                    "page_index": None,
                    "block_id": None,
                    "text": text.strip(),
                    "embedding": None,
                }
            )
        return items

    def _homework_signals(
        self,
        unit: LearningUnit,
        documents: list[Document],
        note_document_ids: set[str],
    ) -> list[dict]:
        document_ids = {
            item.id
            for item in documents
            if item.id not in note_document_ids
            and item.document_kind in {"homework", "corrected_homework", "exam"}
        }
        if not document_ids:
            return []
        questions = self.db.scalars(
            select(Question).where(
                Question.workspace_id == unit.workspace_id,
                Question.document_id.in_(document_ids),
            )
        ).all()
        question_ids = [item.id for item in questions]
        point_by_question = {
            item.question_id: item.knowledge_point_id
            for item in self.db.scalars(
                select(KnowledgePointAttempt).where(
                    KnowledgePointAttempt.workspace_id == unit.workspace_id,
                    KnowledgePointAttempt.learning_unit_id == unit.id,
                    KnowledgePointAttempt.question_id.in_(question_ids),
                )
            ).all()
        } if question_ids else {}
        items = [
            {
                "id": f"question:{question.id}",
                "source_type": "homework_question",
                "authoritative": False,
                "document_id": question.document_id,
                "document_kind": "homework",
                "knowledge_chunk_id": None,
                "question_id": question.id,
                "grading_result_id": None,
                "knowledge_point_id": point_by_question.get(question.id),
                "page_index": self._first_page((question.metadata_ or {}).get("page_refs")),
                "block_id": None,
                "text": question.prompt.strip(),
                "embedding": None,
            }
            for question in questions
            if question.prompt.strip()
        ]
        homeworks = self.db.scalars(
            select(Homework).where(
                Homework.workspace_id == unit.workspace_id,
                Homework.document_id.in_(document_ids),
            )
        ).all()
        homework_by_id = {item.id: item for item in homeworks}
        if homework_by_id:
            gradings = self.db.scalars(
                select(GradingResult).where(
                    GradingResult.workspace_id == unit.workspace_id,
                    GradingResult.homework_id.in_(homework_by_id),
                    GradingResult.feedback.is_not(None),
                )
            ).all()
            for grading in gradings:
                feedback = (grading.feedback or "").strip()
                if not feedback:
                    continue
                items.append(
                    {
                        "id": f"grading:{grading.id}",
                        "source_type": "grading_feedback",
                        "authoritative": False,
                        "document_id": homework_by_id[grading.homework_id].document_id,
                        "document_kind": "homework",
                        "knowledge_chunk_id": None,
                        "question_id": grading.question_id,
                        "grading_result_id": grading.id,
                        "knowledge_point_id": point_by_question.get(grading.question_id),
                        "page_index": None,
                        "block_id": None,
                        "text": feedback,
                        "embedding": None,
                    }
                )
        return items

    def _latest_ocr_text(self, document: Document) -> str | None:
        artifact = self.db.scalar(
            select(DocumentArtifact)
            .where(
                DocumentArtifact.workspace_id == document.workspace_id,
                DocumentArtifact.document_id == document.id,
                DocumentArtifact.artifact_type.in_(("ocr_markdown", "ocr_text")),
            )
            .order_by(DocumentArtifact.created_at.desc())
        )
        if artifact is None:
            return None
        return self.storage.get_text_artifact(artifact.object_key, bucket=artifact.bucket)

    def _query_text(self, unit: LearningUnit, source_blocks: list[dict]) -> str:
        header = "\n".join(
            item for item in (unit.title, unit.subject, unit.grade_level, unit.topic) if item
        )
        manuscript = "\n".join(str(item.get("text") or "") for item in source_blocks)
        value = f"{header}\n{manuscript}".strip()
        return value[: self.settings.note_rewrite_completion_query_max_chars]

    @staticmethod
    def _rank_key(item: dict) -> tuple[float, int, int, str]:
        return (
            float(item.get("relevance_score") or 0),
            1 if item.get("authoritative") else 0,
            SOURCE_PRIORITY.get(str(item.get("source_type") or ""), 0),
            str(item.get("id") or ""),
        )

    @staticmethod
    def _first_page(value) -> int | None:
        if isinstance(value, list) and value and isinstance(value[0], int):
            return max(0, value[0])
        return None

    @staticmethod
    def _selection(unit: LearningUnit, selected: list[dict]) -> NoteCompletionSelection:
        source_document_ids = list(
            dict.fromkeys(
                item["document_id"]
                for item in selected
                if isinstance(item.get("document_id"), str)
            )
        )
        point_ids = list(
            dict.fromkeys(
                item["knowledge_point_id"]
                for item in selected
                if isinstance(item.get("knowledge_point_id"), str)
            )
        )
        digest_parts = [str(unit.knowledge_revision), str(unit.attempt_revision)]
        digest_parts.extend(
            f"{item['id']}:{hashlib.sha256(str(item.get('text') or '').encode('utf-8')).hexdigest()}"
            for item in selected
        )
        digest_input = "|".join(digest_parts)
        return NoteCompletionSelection(
            evidence=selected,
            source_document_ids=source_document_ids,
            knowledge_point_ids=point_ids,
            evidence_revision=hashlib.sha256(digest_input.encode("utf-8")).hexdigest(),
        )
