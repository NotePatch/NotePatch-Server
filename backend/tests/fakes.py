from __future__ import annotations

from pathlib import Path

from notepatch.modules.documents.ocr import OcrBlock, OcrPageResult, OcrPipeline
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    GradingSkillResult,
    KnowledgeBuildResult,
    NoteHighlightResult,
    QuestionExtractionResult,
    ScholarNotesResult,
)


class FakeOcrEngine:
    name = "test-ocr"
    version = "1"

    def recognize(self, image_path: Path, *, page_index: int, options) -> OcrPageResult:
        return OcrPageResult(
            page_index=page_index,
            width=1,
            height=1,
            blocks=[
                OcrBlock(
                    id=f"test-{page_index}",
                    type="text",
                    bbox=(0, 0, 1, 1),
                    confidence=0.99,
                    reading_order=1,
                    text=f"Linear function question on page {page_index + 1}: 2 + 2 = 4",
                )
            ],
        )


def fake_ocr_pipeline() -> OcrPipeline:
    return OcrPipeline(engine=FakeOcrEngine())


class FakeEmbeddingClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def embed(self, texts: list[str], *, owner: str, event_callback=None) -> list[list[float]]:
        vectors = []
        for index, _text in enumerate(texts):
            vector = [0.0] * 1024
            vector[index % 1024] = 1.0
            vectors.append(vector)
        return vectors


class FakeSkillRunner:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute(self, *, task, skill_name, input_payload, output_filename, schema):
        if schema is QuestionExtractionResult:
            payload = {
                "questions": [
                    {
                        "sequence_no": 1,
                        "question_type": "short_answer",
                        "prompt": "What is 2 + 2?",
                        "answer": "4",
                        "page_refs": [0],
                        "evidence": "2 + 2 = 4",
                    }
                ]
            }
        elif schema is KnowledgeBuildResult:
            payload = {
                "chunks": [
                    {
                        "title": "Linear functions",
                        "content": "A linear function has a constant rate of change.",
                        "subject": "math",
                        "grade_level": None,
                        "key_terms": ["linear function"],
                        "page_refs": [0],
                        "difficulty": "medium",
                        "prerequisites": [],
                        "order": 1,
                    }
                ]
            }
        elif schema is ScholarNotesResult:
            document_ids = [item["id"] for item in input_payload.get("documents", [])]
            payload = {
                "title": "Linear Functions Scholar Notes",
                "markdown": "# Linear Functions\n\n**Linear functions** have a constant rate of change.\n",
                "outline": ["Linear Functions"],
                "knowledge_points": ["Linear functions"],
                "review_suggestions": ["Practice slope questions"],
                "source_document_ids": document_ids,
            }
        elif schema is GradingSkillResult:
            mode = input_payload.get("required_grading_mode", "provisional")
            payload = {
                "score": 8,
                "max_score": 10,
                "grading_mode": mode,
                "confidence": 0.8,
                "summary": "The core method is correct but one step needs review.",
                "per_question": [
                    {"sequence_no": 1, "score": 8, "max_score": 10, "feedback": "Review the final step."}
                ],
                "mistakes": [
                    {
                        "knowledge_point": "Linear functions",
                        "description": "The final calculation is incomplete.",
                        "evidence": "2 + 2",
                        "correction": "Complete the arithmetic step.",
                        "recommendation": "Review the highlighted note.",
                        "question_sequence_no": 1,
                    }
                ],
            }
        elif schema is NoteHighlightResult:
            note = input_payload["study_note_markdown"]
            payload = {
                "markdown": note.replace("Linear functions", "**Linear functions**", 1),
                "highlight_map": {
                    "items": [
                        {
                            "mistake_id": item["id"],
                            "knowledge_point": item.get("knowledge_point") or "",
                            "matched_sections": ["Linear Functions"],
                            "confidence": 0.9,
                        }
                        for item in input_payload.get("mistakes", [])
                    ]
                },
            }
        elif schema is FlashcardsSkillResult:
            payload = {
                "flashcards": [
                    {
                        "front": "What characterizes a linear function?",
                        "back": "A constant rate of change.",
                        "knowledge_point": "Linear functions",
                        "source_refs": [],
                        "difficulty": "medium",
                    }
                ]
            }
        else:
            raise AssertionError(f"Unhandled test schema: {schema}")
        return schema.model_validate(payload), {
            "skill": skill_name,
            "output_key": f"test/{task.id}/{output_filename}",
            "input_key": f"test/{task.id}/input.json",
            "gateway_container": "test-gateway",
            "gateway_url": "http://test-gateway:18789",
            "run_result": {"runner": "test"},
        }
