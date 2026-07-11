from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.documents.models.document import Document
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.services.embedding import EmbeddingClient


class KnowledgeService:
    def __init__(self, db: Session, embedding_client: EmbeddingClient | None = None) -> None:
        self.db = db
        self.embedding_client = embedding_client or EmbeddingClient()

    def search(
        self,
        *,
        workspace_id: str,
        query: str,
        learning_unit_id: str | None,
        subject: str | None,
        limit: int,
        owner: str,
    ) -> list[dict]:
        vector = self.embedding_client.embed([query], owner=owner)[0]
        base = select(KnowledgeChunk).outerjoin(Document, Document.id == KnowledgeChunk.document_id).where(
            KnowledgeChunk.workspace_id == workspace_id,
            KnowledgeChunk.embedding.is_not(None),
            (KnowledgeChunk.document_id.is_(None)) | (Document.status != "deleted"),
        )
        if subject:
            base = base.where(KnowledgeChunk.subject == subject)
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            score = (1 - KnowledgeChunk.embedding.cosine_distance(vector)).label("score")
            query_stmt = base.add_columns(score)
            if learning_unit_id:
                query_stmt = query_stmt.where(
                    KnowledgeChunk.metadata_["learning_unit_id"].as_string() == learning_unit_id
                )
            rows = self.db.execute(query_stmt.order_by(score.desc()).limit(limit)).all()
            return [self._result(chunk, float(value)) for chunk, value in rows]

        chunks = self.db.scalars(base).all()
        if learning_unit_id:
            chunks = [
                chunk for chunk in chunks if (chunk.metadata_ or {}).get("learning_unit_id") == learning_unit_id
            ]
        ranked = sorted(
            ((chunk, _cosine_similarity(vector, list(chunk.embedding or []))) for chunk in chunks),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
        return [self._result(chunk, score) for chunk, score in ranked]

    @staticmethod
    def _result(chunk: KnowledgeChunk, score: float) -> dict:
        return {
            "id": chunk.id,
            "workspace_id": chunk.workspace_id,
            "document_id": chunk.document_id,
            "subject": chunk.subject,
            "grade_level": chunk.grade_level,
            "source_type": chunk.source_type,
            "content": chunk.content,
            "metadata": chunk.metadata_ or {},
            "score": score,
            "created_at": chunk.created_at,
        }


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)
