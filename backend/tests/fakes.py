from __future__ import annotations

from pathlib import Path

from notepatch.modules.documents.ocr import OcrBlock, OcrPageResult, OcrPipeline
from notepatch.modules.learning.schemas.skills import (
    FlashcardsSkillResult,
    GradingSkillResult,
    KnowledgeBuildResult,
    NoteHighlightResult,
    NoteSupplementResult,
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

    def execute(
        self,
        *,
        task,
        skill_name,
        input_payload,
        output_filename,
        schema,
        visual_document_ids=None,
    ):
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
            knowledge_points = input_payload.get("knowledge_points", [])
            point = knowledge_points[0]
            source_blocks = input_payload["source_blocks"]
            payload = {
                "title": "Linear Functions Scholar Notes",
                "note_ir": {
                    "summary": "A compact review of linear functions.",
                    "blocks": [
                        {
                            "id": f"note-block-{index}",
                            "type": "text",
                            "source_block_ids": [source["id"]],
                            "source_document_id": source["document_id"],
                            "page_index": source["page_index"],
                            "bbox": source["bbox"],
                            "reading_order": source["reading_order"],
                            "knowledge_point_id": point["id"],
                            "text": source["text"],
                            "confidence": 0.99,
                        }
                        for index, source in enumerate(source_blocks, start=1)
                    ],
                },
                "corrections": [],
                "outline": ["Linear Functions"],
                "knowledge_points": [
                    {"id": point["id"], "name": point["name"], "section_id": "linear-functions"}
                ],
                "review_suggestions": ["Practice slope questions"],
                "source_document_ids": document_ids,
            }

        elif schema is GradingSkillResult:
            mode = input_payload.get("required_grading_mode", "provisional")
            grading_points = input_payload.get("knowledge_points") or [{}]
            payload = {
                "score": 8,
                "max_score": 10,
                "grading_mode": mode,
                "confidence": 0.8,
                "summary": "The core method is correct but one step needs review.",
                "per_question": [
                    {
                        "sequence_no": 1,
                        "score": 8,
                        "max_score": 10,
                        "feedback": "Review the final step.",
                        "knowledge_points": [
                            {
                                "id": grading_points[0].get("id"),
                                "name": "Linear functions",
                            },
                            {
                                "id": grading_points[0].get("id"),
                                "name": "Linear functions",
                            },
                        ],
                    }
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
        elif schema is NoteSupplementResult:
            point = input_payload["knowledge_point"]
            payload = {
                "html": (
                    f'<section id="gap-{point["id"]}" class="np-note-section np-reinforcement" '
                    f'data-knowledge-point-id="{point["id"]}"><h2>{point["name"]}</h2>'
                    "<p>Evidence-backed supplement.</p></section>"
                )
            }
        elif schema is NoteHighlightResult:
            note = input_payload["study_note_html"]
            payload = {
                "html": note.replace(
                    'class="np-note-section np-knowledge-point"',
                    'class="np-note-section np-knowledge-point np-highlight np-highlight--yellow"',
                    1,
                ),
                "highlight_map": {
                    "items": [
                        {
                            "mistake_id": item["id"],
                            "knowledge_point_id": item.get("knowledge_point_id") or input_payload["weighted_knowledge_points"][0]["id"],
                            "knowledge_point": item.get("knowledge_point") or "",
                            "highlight_level": "yellow",
                            "matched_sections": ["Linear Functions"],
                            "confidence": 0.9,
                        }
                        for item in input_payload.get("mistakes", [])
                    ]
                },
            }
        elif schema is FlashcardsSkillResult:
            point = input_payload["weighted_knowledge_points"][0]
            payload = {
                "flashcards": [
                    {
                        "knowledge_point_id": point["id"],
                        "front": "What characterizes a linear function?",
                        "back": "A constant rate of change.",
                        "source_refs": [],
                        "difficulty": "medium",
                    }
                    ,{
                        "knowledge_point_id": point["id"],
                        "front": "How can you recognize a linear function?",
                        "back": "Its rate of change remains constant.",
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
            "visual_reference": {
                "document_ids": list(visual_document_ids or []),
                "selection_policy": "latest_8_image_notes",
                "mode": "multimodal" if visual_document_ids else "none",
            },
        }
