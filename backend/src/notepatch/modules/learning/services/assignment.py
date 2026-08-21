from __future__ import annotations

import tempfile
import unicodedata
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.assignment import LearningUnitAssignment
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument
from notepatch.modules.learning.services.embedding import EmbeddingClient, EmbeddingClientError
from notepatch.modules.learning.services.knowledge import KnowledgeService
from notepatch.platform.config import get_settings
from notepatch.platform.storage import StorageService


ROLE_BY_KIND = {
    "courseware": "courseware",
    "note": "user_note",
    "homework": "homework",
    "corrected_homework": "homework",
    "exam": "exam",
    "answer_key": "answer_key",
    "rubric": "rubric",
}


class LearningUnitAssignmentService:
    def __init__(
        self,
        db: Session,
        *,
        storage: StorageService | None = None,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self.db = db
        self.storage = storage
        self.embedding_client = embedding_client or EmbeddingClient()
        self.settings = get_settings()

    def preassign(self, document: Document) -> LearningUnit | None:
        existing = self.assignment_for_document(document.workspace_id, document.id)
        if existing is not None:
            return self.db.get(LearningUnit, existing.learning_unit_id)

        metadata = dict(document.metadata_ or {})
        explicit_id = metadata.get("learning_unit_id")
        if isinstance(explicit_id, str) and explicit_id:
            unit = self.db.scalar(
                select(LearningUnit).where(
                    LearningUnit.workspace_id == document.workspace_id,
                    LearningUnit.id == explicit_id,
                    LearningUnit.merged_into_id.is_(None),
                )
            )
            if unit is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Learning unit not found")
            return self.assign(document, unit, method="explicit", confidence=1.0, evidence={"field": "learning_unit_id"})

        if metadata.get("auto_group_learning_unit", True) is False:
            return None
        candidates = self._exact_candidates(document)
        if len(candidates) == 1:
            return self.assign(
                document,
                candidates[0],
                method="exact",
                confidence=1.0,
                evidence={"fields": self._provided_match_fields(metadata)},
            )
        return None

    def assign_after_ocr(self, document: Document) -> tuple[LearningUnit, LearningUnitAssignment, str | None]:
        unit = self.preassign(document)
        if unit is not None:
            assignment = self.assignment_for_document(document.workspace_id, document.id)
            return unit, assignment, None

        warning = None
        scores: list[dict] = []
        method = "new"
        selected: LearningUnit | None = None
        confidence: float | None = None
        if self.settings.learning_unit_auto_group_enabled and (document.metadata_ or {}).get("auto_group_learning_unit", True):
            try:
                scores = self._semantic_scores(document)
                if scores:
                    top = scores[0]
                    runner_up = scores[1]["score"] if len(scores) > 1 else 0.0
                    margin = top["score"] - runner_up
                    if (
                        top["score"] >= self.settings.learning_unit_auto_group_threshold
                        and margin >= self.settings.learning_unit_auto_group_min_margin
                    ):
                        selected = self.db.scalar(
                            select(LearningUnit).where(
                                LearningUnit.workspace_id == document.workspace_id,
                                LearningUnit.id == top["learning_unit_id"],
                                LearningUnit.merged_into_id.is_(None),
                            )
                        )
                        if selected is not None:
                            method = "semantic"
                            confidence = float(top["score"])
            except EmbeddingClientError as exc:
                warning = str(exc)

        if selected is None:
            selected = self._create_unit(document)
        self.assign(
            document,
            selected,
            method=method,
            confidence=confidence,
            candidate_scores=scores[:5],
            evidence={"embedding_warning": warning} if warning else {},
        )
        assignment = self.assignment_for_document(document.workspace_id, document.id)
        if assignment is None:
            raise RuntimeError("Learning unit assignment was not persisted")
        return selected, assignment, warning

    def ensure_assigned(self, document: Document) -> LearningUnit:
        unit = self.preassign(document)
        if unit is not None:
            return unit
        link = self.db.scalar(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == document.workspace_id,
                LearningUnitDocument.document_id == document.id,
            )
        )
        if link is not None:
            unit = self.db.get(LearningUnit, link.learning_unit_id)
            if unit is not None:
                self.assign(document, unit, method="existing_link", confidence=1.0)
                return unit
        unit = self._create_unit(document)
        self.assign(document, unit, method="new", confidence=None)
        return unit

    def assign(
        self,
        document: Document,
        unit: LearningUnit,
        *,
        method: str,
        confidence: float | None,
        candidate_scores: list | None = None,
        evidence: dict | None = None,
    ) -> LearningUnit:
        assignment = self.assignment_for_document(document.workspace_id, document.id)
        if assignment is None:
            assignment = LearningUnitAssignment(
                workspace_id=document.workspace_id,
                document_id=document.id,
                learning_unit_id=unit.id,
                method=method,
                confidence=confidence,
                candidate_scores=candidate_scores or [],
                evidence=evidence or {},
            )
            self.db.add(assignment)
        else:
            assignment.learning_unit_id = unit.id
            assignment.method = method
            assignment.confidence = confidence
            assignment.candidate_scores = candidate_scores or []
            assignment.evidence = evidence or {}

        current_links = self.db.scalars(
            select(LearningUnitDocument).where(
                LearningUnitDocument.workspace_id == document.workspace_id,
                LearningUnitDocument.document_id == document.id,
            )
        ).all()
        for link in current_links:
            if link.learning_unit_id != unit.id:
                self.db.delete(link)
        link = next((item for item in current_links if item.learning_unit_id == unit.id), None)
        if link is None:
            self.db.add(
                LearningUnitDocument(
                    workspace_id=document.workspace_id,
                    learning_unit_id=unit.id,
                    document_id=document.id,
                    role=ROLE_BY_KIND.get(document.document_kind, "source"),
                )
            )
        else:
            link.role = ROLE_BY_KIND.get(document.document_kind, "source")

        metadata = dict(document.metadata_ or {})
        metadata["learning_unit_id"] = unit.id
        metadata["learning_unit_assignment"] = {
            "method": method,
            "confidence": confidence,
        }
        document.metadata_ = metadata
        self.db.flush()
        return unit

    def assignment_for_document(self, workspace_id: str, document_id: str) -> LearningUnitAssignment | None:
        return self.db.scalar(
            select(LearningUnitAssignment).where(
                LearningUnitAssignment.workspace_id == workspace_id,
                LearningUnitAssignment.document_id == document_id,
            )
        )

    def _exact_candidates(self, document: Document) -> list[LearningUnit]:
        metadata = document.metadata_ or {}
        title = _normalized(metadata.get("learning_unit_title"))
        topic = _normalized(metadata.get("topic"))
        subject = _normalized(metadata.get("subject"))
        grade = _normalized(metadata.get("grade_level"))
        if not title and not topic:
            return []
        candidates = self.db.scalars(
            select(LearningUnit).where(
                LearningUnit.workspace_id == document.workspace_id,
                LearningUnit.merged_into_id.is_(None),
            )
        ).all()
        matched = []
        for unit in candidates:
            if title and _normalized(unit.title) != title:
                continue
            if topic and _normalized(unit.topic) != topic:
                continue
            if subject and _normalized(unit.subject) not in {"", subject}:
                continue
            if grade and _normalized(unit.grade_level) not in {"", grade}:
                continue
            matched.append(unit)
        return matched

    def _semantic_scores(self, document: Document) -> list[dict]:
        has_candidates = self.db.scalar(
            select(KnowledgeChunk.id).where(
                KnowledgeChunk.workspace_id == document.workspace_id,
                KnowledgeChunk.embedding.is_not(None),
            ).limit(1)
        )
        if has_candidates is None:
            return []
        text = self._grouping_text(document)
        items = KnowledgeService(self.db, self.embedding_client).search(
            workspace_id=document.workspace_id,
            query=text,
            learning_unit_id=None,
            subject=_string(document.metadata_.get("subject")),
            limit=50,
            owner=f"document:{document.id}:unit-assignment",
        )
        by_unit: dict[str, dict] = {}
        for item in items:
            unit_id = (item.get("metadata") or {}).get("learning_unit_id")
            if not isinstance(unit_id, str):
                continue
            unit = self.db.scalar(
                select(LearningUnit).where(
                    LearningUnit.workspace_id == document.workspace_id,
                    LearningUnit.id == unit_id,
                    LearningUnit.merged_into_id.is_(None),
                )
            )
            if unit is None or self._metadata_conflicts(document, unit):
                continue
            current = by_unit.get(unit_id)
            if current is None or item["score"] > current["score"]:
                by_unit[unit_id] = {
                    "learning_unit_id": unit_id,
                    "title": unit.title,
                    "score": round(float(item["score"]), 6),
                }
        return sorted(by_unit.values(), key=lambda item: item["score"], reverse=True)

    def _grouping_text(self, document: Document) -> str:
        metadata = document.metadata_ or {}
        parts = [
            document.title,
            document.original_filename,
            _string(metadata.get("learning_unit_title")),
            _string(metadata.get("subject")),
            _string(metadata.get("grade_level")),
            _string(metadata.get("topic")),
            self._ocr_text(document),
        ]
        return "\n".join(part for part in parts if part)[: self.settings.learning_unit_grouping_text_max_chars]

    def _ocr_text(self, document: Document) -> str:
        if self.storage is None:
            return ""
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
            return ""
        with tempfile.TemporaryDirectory(prefix="notepatch-unit-assignment-") as tmpdir:
            target = Path(tmpdir) / "ocr.txt"
            self.storage.download_file(artifact.bucket, artifact.object_key, target)
            return target.read_text(encoding="utf-8", errors="ignore")

    def _create_unit(self, document: Document) -> LearningUnit:
        metadata = document.metadata_ or {}
        title = (
            _string(metadata.get("learning_unit_title"))
            or document.title
            or document.original_filename
            or "学习资料"
        )
        unit = LearningUnit(
            workspace_id=document.workspace_id,
            title=title[:255],
            subject=_string(metadata.get("subject")),
            grade_level=_string(metadata.get("grade_level")),
            topic=_string(metadata.get("topic")),
            metadata_={"source": "automatic_pipeline", "source_document_id": document.id},
        )
        self.db.add(unit)
        self.db.flush()
        return unit

    @staticmethod
    def _metadata_conflicts(document: Document, unit: LearningUnit) -> bool:
        metadata = document.metadata_ or {}
        subject = _normalized(metadata.get("subject"))
        grade = _normalized(metadata.get("grade_level"))
        return bool(
            (subject and _normalized(unit.subject) and subject != _normalized(unit.subject))
            or (grade and _normalized(unit.grade_level) and grade != _normalized(unit.grade_level))
        )

    @staticmethod
    def _provided_match_fields(metadata: dict) -> list[str]:
        return [
            field
            for field in ("learning_unit_title", "subject", "grade_level", "topic")
            if _string(metadata.get(field))
        ]


def _string(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _normalized(value) -> str:
    text = _string(value)
    if not text:
        return ""
    return "".join(
        character.lower()
        for character in unicodedata.normalize("NFKC", text)
        if character.isalnum()
    )
