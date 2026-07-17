from __future__ import annotations

import math
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.learning.models.learning import KnowledgePoint, LearningUnit
from notepatch.modules.learning.services.embedding import EmbeddingClient


def normalize_knowledge_point_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)


def cosine_similarity(left, right) -> float:
    left_values = [float(item) for item in left]
    right_values = [float(item) for item in right]
    dot = sum(a * b for a, b in zip(left_values, right_values, strict=True))
    left_norm = math.sqrt(sum(item * item for item in left_values))
    right_norm = math.sqrt(sum(item * item for item in right_values))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


class KnowledgePointService:
    def __init__(self, db: Session, embedding_client: EmbeddingClient, *, match_threshold: float) -> None:
        self.db = db
        self.embedding_client = embedding_client
        self.match_threshold = match_threshold

    def resolve_many(
        self,
        *,
        unit: LearningUnit,
        names: list[str],
        source_document_ids: list[str] | None = None,
        vectors: dict[str, list[float]] | None = None,
        owner: str,
    ) -> dict[str, KnowledgePoint]:
        clean_names = list(dict.fromkeys(name.strip() for name in names if name and name.strip()))
        if not clean_names:
            return {}
        supplied = vectors or {}
        missing = [name for name in clean_names if name not in supplied]
        generated = self.embedding_client.embed(missing, owner=owner) if missing else []
        vector_by_name = {**supplied, **dict(zip(missing, generated, strict=True))}
        existing = self.db.scalars(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == unit.workspace_id,
                KnowledgePoint.learning_unit_id == unit.id,
            )
        ).all()
        by_normalized = {item.normalized_name: item for item in existing}
        resolved: dict[str, KnowledgePoint] = {}
        for name in clean_names:
            normalized = normalize_knowledge_point_name(name)
            if not normalized:
                continue
            point = by_normalized.get(normalized)
            vector = vector_by_name[name]
            if point is None:
                point = self._semantic_match(existing, vector)
            if point is None:
                point = KnowledgePoint(
                    workspace_id=unit.workspace_id,
                    learning_unit_id=unit.id,
                    name=name,
                    normalized_name=normalized,
                    embedding=vector,
                    source_document_ids=list(source_document_ids or []),
                    metadata_={},
                )
                self.db.add(point)
                self.db.flush()
                existing.append(point)
                by_normalized[normalized] = point
            else:
                point.source_document_ids = list(
                    dict.fromkeys([*(point.source_document_ids or []), *(source_document_ids or [])])
                )
                if point.embedding is None:
                    point.embedding = vector
            resolved[name] = point
        return resolved

    def resolve_references(
        self,
        *,
        unit: LearningUnit,
        references: list,
        owner: str,
    ) -> list[KnowledgePoint]:
        existing_by_id = {
            item.id: item
            for item in self.db.scalars(
                select(KnowledgePoint).where(
                    KnowledgePoint.workspace_id == unit.workspace_id,
                    KnowledgePoint.learning_unit_id == unit.id,
                )
            ).all()
        }
        resolved: list[KnowledgePoint] = []
        unresolved_names: list[str] = []
        for reference in references:
            reference_id = getattr(reference, "id", None)
            if reference_id and reference_id in existing_by_id:
                resolved.append(existing_by_id[reference_id])
            else:
                unresolved_names.append(reference.name)
        by_name = self.resolve_many(unit=unit, names=unresolved_names, owner=owner)
        for reference in references:
            reference_id = getattr(reference, "id", None)
            point = existing_by_id.get(reference_id) if reference_id else None
            point = point or by_name.get(reference.name)
            if point is not None and point not in resolved:
                resolved.append(point)
        return resolved

    def _semantic_match(self, existing: list[KnowledgePoint], vector: list[float]) -> KnowledgePoint | None:
        candidates = [item for item in existing if item.embedding is not None]
        if not candidates:
            return None
        scored = [(cosine_similarity(item.embedding, vector), item) for item in candidates]
        score, point = max(scored, key=lambda item: item[0])
        return point if score >= self.match_threshold else None
