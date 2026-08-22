from __future__ import annotations

import pytest
from sqlalchemy import select

from notepatch.modules.documents.models.document import Document, DocumentArtifact
from notepatch.modules.learning.models.homework import Question
from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import (
    KnowledgePoint,
    LearningUnit,
    LearningUnitDocument,
    StudyNoteVersion,
)
from notepatch.modules.learning.schemas.skills import ScholarNotesResult
from notepatch.modules.learning.services.note_completion import NoteCompletionEvidenceService
from notepatch.modules.learning.services.note_ir import (
    bind_note_evidence_refs,
    render_note_ir,
    validate_note_ir,
)
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from tests.conftest import first_workspace_id, register_user


def _vector(first: float, second: float) -> list[float]:
    return [first, second, *([0.0] * 1022)]


class TopicEmbeddingClient:
    def embed(self, texts, *, owner, event_callback=None):
        vectors = []
        for text in texts:
            normalized = text.casefold()
            if any(word in normalized for word in ("cpu", "register", "bus")):
                vectors.append(_vector(1.0, 0.0))
            elif any(word in normalized for word in ("plant", "photosynthesis")):
                vectors.append(_vector(0.0, 1.0))
            else:
                vectors.append(_vector(0.0, 0.0))
        return vectors


def _document(workspace_id: str, user_id: str, title: str, kind: str) -> Document:
    return Document(
        workspace_id=workspace_id,
        uploaded_by=user_id,
        title=title,
        original_filename=f"{title}.png",
        mime_type="image/png",
        file_size=100,
        file_type="image",
        document_kind=kind,
        bucket="notepatch-test",
        object_key=f"tests/{title}.png",
        status="ready",
        metadata_={},
    )


def test_rewrite_completion_selects_related_unit_evidence_without_trusting_student_answer(
    client, db_sessionmaker, fake_storage, monkeypatch
):
    registered = register_user(client, "rewrite-evidence@example.com")
    workspace_id = first_workspace_id(client, registered["access_token"])
    user_id = registered["user"]["id"]
    monkeypatch.setattr(get_settings(), "note_rewrite_completion_similarity_threshold", 0.75)
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="CPU architecture")
        note = _document(workspace_id, user_id, "CPU overview", "note")
        courseware = _document(workspace_id, user_id, "CPU registers", "courseware")
        unrelated = _document(workspace_id, user_id, "Plant biology", "courseware")
        homework = _document(workspace_id, user_id, "CPU homework", "homework")
        db.add_all([unit, note, courseware, unrelated, homework])
        db.flush()
        related_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=courseware.id,
            source_type="openclaw_skill",
            content="CPU registers include the PC, CIR, MAR, MDR, and accumulator.",
            embedding=_vector(1.0, 0.0),
            metadata_={"learning_unit_id": unit.id, "knowledge_point_id": "point-registers"},
        )
        unrelated_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=unrelated.id,
            source_type="openclaw_skill",
            content="Photosynthesis converts light energy in plants.",
            embedding=_vector(0.0, 1.0),
            metadata_={"learning_unit_id": unit.id, "knowledge_point_id": "point-plants"},
        )
        question = Question(
            workspace_id=workspace_id,
            document_id=homework.id,
            sequence_no=1,
            prompt="Explain the role of the CPU program counter register.",
            answer="The CPU is made of bananas.",
            metadata_={},
        )
        db.add_all([related_chunk, unrelated_chunk, question])
        db.commit()

        selection = NoteCompletionEvidenceService(
            db, fake_storage, TopicEmbeddingClient()
        ).select(
            unit=unit,
            documents=[note, courseware, unrelated, homework],
            note_documents=[note],
            source_blocks=[
                {
                    "id": "note:cpu",
                    "document_id": note.id,
                    "text": "The CPU processes instructions.",
                }
            ],
            chunks=[related_chunk, unrelated_chunk],
            owner="test:completion",
        )

    evidence_ids = {item["id"] for item in selection.evidence}
    assert f"chunk:{related_chunk.id}" in evidence_ids
    assert f"question:{question.id}" in evidence_ids
    assert f"chunk:{unrelated_chunk.id}" not in evidence_ids
    assert all("bananas" not in item["text"] for item in selection.evidence)
    assert selection.source_document_ids == [courseware.id, homework.id]


def _rewrite_result(point_id: str, evidence_id: str) -> ScholarNotesResult:
    return ScholarNotesResult.model_validate(
        {
            "title": "CPU notes",
            "note_ir": {
                "summary": "CPU overview with related completion.",
                "blocks": [
                    {
                        "id": "manuscript-1",
                        "type": "text",
                        "source_block_ids": ["source-1"],
                        "source_document_id": "note-document",
                        "page_index": 0,
                        "bbox": [0, 0, 100, 20],
                        "reading_order": 1,
                        "knowledge_point_id": point_id,
                        "text": "The CPU processes instructions.",
                    },
                    {
                        "id": "supplement-1",
                        "type": "text",
                        "origin": "evidence_supplement",
                        "source_refs": [{"evidence_id": evidence_id}],
                        "supplement_reason": "The source note introduces the CPU but omits its registers.",
                        "page_index": 0,
                        "bbox": [0, 0, 1, 1],
                        "reading_order": 2,
                        "knowledge_point_id": point_id,
                        "text": "The PC, CIR, MAR, MDR, and ACC are key CPU registers.",
                    },
                ],
            },
            "knowledge_points": [
                {"id": point_id, "name": "CPU registers", "section_id": "cpu-registers"}
            ],
            "source_document_ids": ["note-document"],
        }
    )


def test_evidence_supplement_is_rewrite_only_authoritative_and_rendered_with_source_marker():
    source_blocks = [
        {
            "id": "source-1",
            "document_id": "note-document",
            "page_index": 0,
            "bbox": [0, 0, 100, 20],
            "reading_order": 1,
            "text": "The CPU processes instructions.",
        }
    ]
    result = _rewrite_result("point-cpu", "chunk:registers")
    evidence = {
        "chunk:registers": {
            "id": "chunk:registers",
            "source_type": "knowledge_chunk",
            "authoritative": True,
            "document_id": "courseware-document",
            "knowledge_chunk_id": "chunk-id",
            "question_id": None,
            "grading_result_id": None,
            "page_index": 2,
            "block_id": None,
            "excerpt": "CPU registers include PC, CIR, MAR, MDR, and ACC.",
            "relevance_score": 0.94,
        }
    }

    validate_note_ir(
        result,
        source_blocks,
        content_edit_level="rewrite",
        layout_edit_level="reflow",
        completion_evidence=evidence,
    )
    bind_note_evidence_refs(result, evidence)
    html = render_note_ir(result)
    supplement = result.note_ir.blocks[1]

    assert "np-evidence-supplement" in html
    assert "np-supplement-badge" in html
    assert "资料补充" in html
    assert supplement.source_refs[0].document_id == "courseware-document"
    assert supplement.source_refs[0].knowledge_chunk_id == "chunk-id"
    assert supplement.source_refs[0].relevance_score == 0.94

    with pytest.raises(ValueError, match="only allowed in rewrite"):
        validate_note_ir(
            _rewrite_result("point-cpu", "chunk:registers"),
            source_blocks,
            content_edit_level="conceptual",
            layout_edit_level="minor",
            completion_evidence=evidence,
        )


def test_homework_signal_alone_cannot_support_rewrite_supplement():
    result = _rewrite_result("point-cpu", "question:1")
    with pytest.raises(ValueError, match="authoritative"):
        validate_note_ir(
            result,
            [
                {
                    "id": "source-1",
                    "document_id": "note-document",
                    "page_index": 0,
                    "bbox": [0, 0, 100, 20],
                    "reading_order": 1,
                    "text": "The CPU processes instructions.",
                }
            ],
            content_edit_level="rewrite",
            layout_edit_level="reflow",
            completion_evidence={
                "question:1": {
                    "id": "question:1",
                    "source_type": "homework_question",
                    "authoritative": False,
                    "document_id": "homework-document",
                    "excerpt": "Name two CPU registers.",
                    "relevance_score": 0.9,
                }
            },
        )


def test_rewrite_reflow_rejects_one_to_one_ocr_copy():
    source_blocks = [
        {
            "id": "source-1",
            "document_id": "note-document",
            "page_index": 0,
            "bbox": [0, 0, 100, 20],
            "reading_order": 1,
            "text": "Registers",
        },
        {
            "id": "source-2",
            "document_id": "note-document",
            "page_index": 0,
            "bbox": [0, 30, 100, 50],
            "reading_order": 2,
            "text": "PC: next instruction",
        },
    ]
    result = ScholarNotesResult.model_validate(
        {
            "title": "Registers",
            "note_ir": {
                "summary": "CPU registers.",
                "blocks": [
                    {
                        "id": "block-1",
                        "type": "text",
                        "source_block_ids": ["source-1"],
                        "source_document_id": "note-document",
                        "page_index": 0,
                        "bbox": [0, 0, 100, 20],
                        "reading_order": 1,
                        "knowledge_point_id": "point-registers",
                        "text": "Registers",
                    },
                    {
                        "id": "block-2",
                        "type": "text",
                        "source_block_ids": ["source-2"],
                        "source_document_id": "note-document",
                        "page_index": 0,
                        "bbox": [0, 30, 100, 50],
                        "reading_order": 2,
                        "knowledge_point_id": "point-registers",
                        "text": "PC: next instruction",
                    },
                ],
            },
            "knowledge_points": [
                {
                    "id": "point-registers",
                    "name": "CPU registers",
                    "section_id": "registers",
                }
            ],
            "source_document_ids": ["note-document"],
        }
    )

    with pytest.raises(ValueError, match="must consolidate related source blocks"):
        validate_note_ir(
            result,
            source_blocks,
            content_edit_level="rewrite",
            layout_edit_level="reflow",
        )

    result.note_ir.blocks[0].source_block_ids = ["source-1", "source-2"]
    result.note_ir.blocks[0].text = (
        "CPU registers hold the values needed while instructions are processed; "
        "the program counter identifies the next instruction."
    )
    result.note_ir.blocks.pop()
    validate_note_ir(
        result,
        source_blocks,
        content_edit_level="rewrite",
        layout_edit_level="reflow",
    )


def test_rewrite_note_workflow_persists_evidence_completion_and_provenance(
    client, db_sessionmaker, fake_storage
):
    registered = register_user(client, "rewrite-workflow@example.com")
    workspace_id = first_workspace_id(client, registered["access_token"])
    user_id = registered["user"]["id"]
    with db_sessionmaker() as db:
        unit = LearningUnit(
            workspace_id=workspace_id,
            title="CPU architecture",
            knowledge_revision=2,
        )
        note = _document(workspace_id, user_id, "CPU overview", "note")
        note.file_type = "pdf"
        courseware = _document(workspace_id, user_id, "CPU registers", "courseware")
        db.add_all([unit, note, courseware])
        db.flush()
        db.add_all(
            [
                LearningUnitDocument(
                    workspace_id=workspace_id,
                    learning_unit_id=unit.id,
                    document_id=note.id,
                    role="user_note",
                ),
                LearningUnitDocument(
                    workspace_id=workspace_id,
                    learning_unit_id=unit.id,
                    document_id=courseware.id,
                    role="courseware",
                ),
            ]
        )
        note_point = KnowledgePoint(
            id="point-cpu-overview",
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            name="CPU overview",
            normalized_name="cpuoverview",
            source_document_ids=[note.id],
            metadata_={},
        )
        register_point = KnowledgePoint(
            id="point-cpu-registers",
            workspace_id=workspace_id,
            learning_unit_id=unit.id,
            name="CPU registers",
            normalized_name="cpuregisters",
            source_document_ids=[courseware.id],
            metadata_={},
        )
        note_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=note.id,
            source_type="openclaw_skill",
            content="The CPU processes instructions.",
            embedding=_vector(1.0, 0.0),
            metadata_={"learning_unit_id": unit.id, "knowledge_point_id": note_point.id},
        )
        register_chunk = KnowledgeChunk(
            workspace_id=workspace_id,
            document_id=courseware.id,
            source_type="openclaw_skill",
            content="CPU registers include PC, CIR, MAR, MDR, and ACC.",
            embedding=_vector(1.0, 0.0),
            metadata_={"learning_unit_id": unit.id, "knowledge_point_id": register_point.id},
        )
        artifact = DocumentArtifact(
            workspace_id=workspace_id,
            document_id=note.id,
            artifact_type="ocr_json",
            bucket=fake_storage.bucket,
            object_key=f"ocr/{note.id}.json",
            mime_type="application/json",
            metadata_={},
        )
        db.add_all([note_point, register_point, note_chunk, register_chunk, artifact])
        db.flush()
        fake_storage.put_json_artifact(
            artifact.object_key,
            {
                "pages": [
                    {
                        "page_index": 0,
                        "blocks": [
                            {
                                "id": "cpu-definition",
                                "type": "text",
                                "bbox": [0, 0, 100, 20],
                                "reading_order": 1,
                                "text": "The CPU processes instructions.",
                                "confidence": 0.99,
                            }
                        ],
                    }
                ]
            },
            bucket=fake_storage.bucket,
        )
        db.commit()
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="generate_study_notes",
            resource_type="learning_unit",
            resource_id=unit.id,
            payload={
                "learning_unit_id": unit.id,
                "expected_knowledge_revision": unit.knowledge_revision,
                "expected_attempt_revision": unit.attempt_revision,
                "content_edit_level": "rewrite",
                "layout_edit_level": "reflow",
                "history_limit": 3,
            },
            enqueue=False,
        )
        completed = process_task(db, task.id, storage=fake_storage)
        assert completed.status == "succeeded", completed.error_message
        saved = db.scalar(
            select(StudyNoteVersion).where(StudyNoteVersion.task_id == task.id)
        )
        assert saved is not None
        assert saved.metadata_["completion_count"] == 1
        assert saved.metadata_["completion_source_document_ids"] == [courseware.id]
        assert saved.metadata_["completion_strategy"] == "rewrite_related_evidence_v1"
        assert set(saved.source_document_ids) == {note.id, courseware.id}
        html = fake_storage.get_text_artifact(saved.html_object_key)
        note_json = fake_storage.get_json_artifact(saved.json_object_key)
        supplement = next(
            block
            for block in note_json["note_ir"]["blocks"]
            if block["origin"] == "evidence_supplement"
        )
        assert "np-evidence-supplement" in html
        assert supplement["source_refs"][0]["document_id"] == courseware.id
        assert supplement["source_refs"][0]["knowledge_chunk_id"] == register_chunk.id
