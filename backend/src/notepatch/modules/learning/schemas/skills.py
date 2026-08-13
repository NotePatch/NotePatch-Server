from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedQuestion(StrictResult):
    sequence_no: int = Field(ge=1)
    question_type: str | None = Field(default=None, max_length=255)
    prompt: str = Field(min_length=1)
    answer: str | None = None
    page_refs: list[int] = Field(default_factory=list)
    evidence: str | None = None


class QuestionExtractionResult(StrictResult):
    questions: list[ExtractedQuestion] = Field(min_length=1)


class KnowledgeChunkResult(StrictResult):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    subject: str | None = None
    grade_level: str | None = None
    key_terms: list[str] = Field(default_factory=list)
    page_refs: list[int] = Field(default_factory=list)
    difficulty: str | None = None
    prerequisites: list[str] = Field(default_factory=list)
    order: int = Field(ge=1)


class KnowledgeBuildResult(StrictResult):
    chunks: list[KnowledgeChunkResult] = Field(min_length=1)


class KnowledgePointReference(StrictResult):
    id: str | None = None
    name: str = Field(min_length=1)


class ScholarKnowledgePoint(StrictResult):
    id: str
    name: str = Field(min_length=1)
    section_id: str = Field(min_length=1)


class ScholarNotesResult(StrictResult):
    title: str = Field(min_length=1)
    html: str = Field(min_length=1)
    outline: list[str] = Field(default_factory=list)
    knowledge_points: list[ScholarKnowledgePoint] = Field(min_length=1)
    review_suggestions: list[str] = Field(default_factory=list)
    source_document_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_html_contract(self):
        from notepatch.modules.learning.services.html_notes import (
            sanitize_note_html,
            validate_knowledge_point_references,
            validate_note_structure,
        )

        cleaned = sanitize_note_html(self.html)
        validate_note_structure(cleaned)
        allowed_ids = {item.id for item in self.knowledge_points}
        validate_knowledge_point_references(cleaned, allowed_ids)
        referenced = {item.id for item in self.knowledge_points}
        missing_sections = referenced - set(
            __import__("re").findall(r'data-knowledge-point-id="([^"]+)"', cleaned)
        )
        if missing_sections:
            raise ValueError(f"Scholar note sections are missing: {', '.join(sorted(missing_sections))}")
        self.html = cleaned
        return self


class PerQuestionGrade(StrictResult):
    sequence_no: int = Field(ge=1)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    feedback: str = Field(min_length=1)
    evidence: str | None = None
    knowledge_points: list[KnowledgePointReference] = Field(min_length=1)


class GradingMistake(StrictResult):
    knowledge_point: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence: str | None = None
    correction: str | None = None
    recommendation: str | None = None
    question_sequence_no: int | None = Field(default=None, ge=1)


class GradingSkillResult(StrictResult):
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    grading_mode: Literal["official", "provisional"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1)
    per_question: list[PerQuestionGrade] = Field(default_factory=list)
    mistakes: list[GradingMistake] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_within_maximum(self):
        if self.score > self.max_score:
            raise ValueError("score cannot exceed max_score")
        return self


class HighlightMapItem(StrictResult):
    mistake_id: str
    knowledge_point_id: str
    knowledge_point: str
    highlight_level: Literal["red", "yellow"]
    matched_sections: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class HighlightMap(StrictResult):
    items: list[HighlightMapItem] = Field(default_factory=list)


class NoteHighlightResult(StrictResult):
    html: str = Field(min_length=1)
    highlight_map: HighlightMap


class FlashcardResult(StrictResult):
    knowledge_point_id: str
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)
    source_refs: list[str] = Field(default_factory=list)
    difficulty: str | None = None


class FlashcardsSkillResult(StrictResult):
    flashcards: list[FlashcardResult] = Field(min_length=1)
