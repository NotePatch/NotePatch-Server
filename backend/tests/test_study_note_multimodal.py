import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from sqlalchemy import select

from notepatch.modules.ai.services.gateway import OpenClawGatewayRunner, OpenClawRunnerError
from notepatch.modules.ai.services.skill_runner import OpenClawSkillRunner
from notepatch.modules.ai.services.visual_attachments import VisualAttachmentBuilder
from notepatch.modules.learning.schemas.learning import StudyNoteVersionRead
from notepatch.modules.learning.schemas.skills import QuestionExtractionResult
from notepatch.modules.learning.services.content_operations import LearningContentOperations
from notepatch.modules.learning.models.learning import StudyNoteVersion
from notepatch.modules.learning.services.html_notes import sanitize_note_html
from notepatch.modules.tasks.models.task import TaskEvent
from notepatch.modules.tasks.services.executor import process_task
from notepatch.modules.tasks.services.task import TaskService
from tests.conftest import first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient
from tests.test_learning_workflow import (
    _create_and_complete_document,
    _latest_task,
    _process_assignment_if_present,
    _set_auto_learning,
)


def _make_image(path: Path, *, image_format: str = "PNG", size: tuple[int, int] = (320, 240)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (245, 242, 231)).save(path, format=image_format)


def test_visual_attachment_builder_uses_direct_images_and_task_local_tiff_preview(tmp_path):
    input_dir = tmp_path / "input"
    documents_dir = input_dir / "documents"
    png_path = documents_dir / "png-note" / "original" / "note.png"
    tiff_path = documents_dir / "tiff-note" / "original" / "note.tiff"
    _make_image(png_path)
    _make_image(tiff_path, image_format="TIFF")
    runtime = {
        "host_task_input_dir": str(input_dir),
        "documents_root_path": "/workspace/notepatch/openclaw/tasks/task/input/documents",
        "document_contexts": {
            "png-note": {
                "document_id": "png-note",
                "filename": "note.png",
                "title": "PNG note",
                "mime_type": "image/png",
                "file_type": "image",
                "original_path": "/workspace/notepatch/openclaw/tasks/task/input/documents/png-note/original/note.png",
            },
            "tiff-note": {
                "document_id": "tiff-note",
                "filename": "note.tiff",
                "title": "TIFF note",
                "mime_type": "image/tiff",
                "file_type": "image",
                "original_path": "/workspace/notepatch/openclaw/tasks/task/input/documents/tiff-note/original/note.tiff",
            },
        },
    }

    direct = VisualAttachmentBuilder().build(runtime, ["png-note"])
    assert direct.used_previews is False
    assert direct.attachments[0]["original_path"].endswith("/png-note/original/note.png")

    converted = VisualAttachmentBuilder().build(runtime, ["png-note", "tiff-note"])
    assert converted.used_previews is True
    assert converted.document_ids == ["png-note", "tiff-note"]
    assert all(item["mime_type"] == "image/jpeg" for item in converted.attachments)
    assert all("/.visual-previews/" in item["original_path"] for item in converted.attachments)


def test_visual_note_selection_uses_latest_eight_eligible_images():
    documents = [
        SimpleNamespace(id=f"note-{index}", document_kind="note", file_type="image", status="ready")
        for index in range(10)
    ]
    documents.extend(
        [
            SimpleNamespace(id="courseware", document_kind="courseware", file_type="image", status="ready"),
            SimpleNamespace(id="failed", document_kind="note", file_type="image", status="failed"),
            SimpleNamespace(id="pdf-note", document_kind="note", file_type="pdf", status="ready"),
        ]
    )
    selected = LearningContentOperations._visual_note_documents(documents)
    assert [item.id for item in selected] == [f"note-{index}" for index in range(2, 10)]


class VisualRuntimeStub:
    def __init__(self, root: Path) -> None:
        self.root = root

    def sync_workspace_documents(self, *, db, storage, workspace_id, task_id, model_ids=None):
        input_dir = self.root / task_id / "input"
        output_dir = self.root / task_id / "output"
        image_path = input_dir / "documents" / "image-note" / "original" / "note.png"
        _make_image(image_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        container_root = f"/workspace/notepatch/openclaw/tasks/{task_id}/input/documents"
        return {
            "gateway_url": "http://gateway:18789",
            "gateway_token": "token",
            "container_name": "gateway",
            "workspace_dir": str(self.root),
            "documents_index_path": f"{container_root}/index.json",
            "documents_root_path": container_root,
            "task_output_path": f"/workspace/notepatch/openclaw/tasks/{task_id}/output",
            "host_task_input_dir": str(input_dir),
            "host_task_output_dir": str(output_dir),
            "mirrored_document_ids": ["image-note"],
            "document_contexts": {
                "image-note": {
                    "document_id": "image-note",
                    "filename": "note.png",
                    "title": "Image note",
                    "mime_type": "image/png",
                    "file_type": "image",
                    "original_path": f"{container_root}/image-note/original/note.png",
                }
            },
        }


class TextOnlyThenSuccessRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def prepare_task_dir(self, workspace_id, task_id):
        return Path("/tmp")

    def run_task(self, workspace_id, task_id, payload):
        self.calls.append(payload)
        if payload.get("input", {}).get("attachments"):
            raise OpenClawRunnerError(
                'OpenClaw gateway returned HTTP 400: {"error":{"message":"model does not support image input"}}'
            )
        output = Path(payload["_openclaw"]["host_task_output_dir"]) / "questions.json"
        output.write_text(
            json.dumps(
                {
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
            ),
            encoding="utf-8",
        )
        return {"runner": "test", "answer": "done"}


def test_skill_runner_falls_back_only_for_explicit_visual_capability_error(
    client, db_sessionmaker, fake_storage, tmp_path
):
    user = register_user(client, "visual-fallback@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    runner = TextOnlyThenSuccessRunner()
    with db_sessionmaker() as db:
        task = TaskService(db).create_task(
            workspace_id=workspace_id,
            task_type="extract_questions",
            resource_type="document",
            payload={"ai_model": "openai/text-only"},
            enqueue=False,
        )
        result, metadata = OpenClawSkillRunner(
            db=db,
            storage=fake_storage,
            gateway_runner=runner,
            runtime_service=VisualRuntimeStub(tmp_path),
        ).execute(
            task=task,
            skill_name="notepatch_question_extractor",
            input_payload={"ocr_text": "2 + 2 = 4"},
            output_filename="questions.json",
            schema=QuestionExtractionResult,
            visual_document_ids=["image-note"],
        )
        events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all()

    assert result.questions[0].answer == "4"
    assert len(runner.calls) == 2
    assert runner.calls[0]["input"]["attachments"][0]["document_id"] == "image-note"
    assert "attachments" not in runner.calls[1]["input"]
    assert metadata["visual_reference"]["mode"] == "ocr_only_fallback"
    assert "study_note_visual_fallback" in {event.event_type for event in events}


def test_skill_runner_payload_supports_real_gateway_image_resolution(tmp_path, fake_storage):
    runtime = VisualRuntimeStub(tmp_path).sync_workspace_documents(
        db=None,
        storage=fake_storage,
        workspace_id="workspace-1",
        task_id="task-1",
    )
    visual = VisualAttachmentBuilder().build(runtime, ["image-note"])
    payload = OpenClawSkillRunner._request_payload(
        task=SimpleNamespace(),
        runtime=runtime,
        skill_name="notepatch_scholar_notes",
        output_filename="study_notes.json",
        session_key="notepatch:workspace-1:task-1",
        provider_model="openai/test-model",
        timeout_seconds=30,
        attachments=visual.attachments,
    )

    parts = OpenClawGatewayRunner._image_parts(payload["input"], payload["_openclaw"])

    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    assert payload["_openclaw"]["host_task_input_dir"] == runtime["host_task_input_dir"]




def test_image_note_workflow_records_multimodal_visual_provenance(
    client, db_sessionmaker, fake_storage
):
    settings, old_auto = _set_auto_learning(True)
    try:
        user = register_user(client, "image-note-visual@example.com")
        workspace_id = first_workspace_id(client, user["access_token"])
        upload = _create_and_complete_document(
            client,
            fake_storage,
            user["access_token"],
            workspace_id,
            filename="handwritten-note.png",
            document_kind="note",
        )
        document_id = upload["document"]["id"]
        with db_sessionmaker() as db:
            process_task(
                db,
                _latest_task(db, workspace_id, "document_processing_pipeline", document_id).id,
                storage=fake_storage,
                doctr_client=FailingDocTrClient(),
            )
            _process_assignment_if_present(db, workspace_id, document_id, fake_storage)
            process_task(
                db,
                _latest_task(db, workspace_id, "build_knowledge_base", document_id).id,
                storage=fake_storage,
            )
            note_task = _latest_task(
                db,
                workspace_id,
                "generate_study_notes",
                _latest_task(db, workspace_id, "build_knowledge_base", document_id).payload[
                    "learning_unit_id"
                ],
            )
            process_task(db, note_task.id, storage=fake_storage)
            note = db.scalar(
                select(StudyNoteVersion).where(StudyNoteVersion.workspace_id == workspace_id)
            )

        assert note is not None
        assert note.metadata_["theme_id"] == "notepatch-paper-v2"
        assert note.metadata_["visual_reference_document_ids"] == [document_id]
        assert note.metadata_["visual_reference_mode"] == "multimodal"
    finally:
        settings.auto_learning_pipeline = old_auto

def test_visual_fallback_does_not_mask_gateway_or_auth_failures():
    assert OpenClawSkillRunner._is_visual_capability_error(
        OpenClawRunnerError("OpenClaw gateway returned HTTP 400: model does not support image input")
    )
    assert not OpenClawSkillRunner._is_visual_capability_error(
        OpenClawRunnerError("OpenClaw gateway returned HTTP 500: internal error")
    )
    assert not OpenClawSkillRunner._is_visual_capability_error(
        OpenClawRunnerError("OpenClaw gateway returned HTTP 401: invalid token")
    )

def test_v2_note_theme_and_layout_classes_are_safe(client):
    response = client.get("/api/v1/assets/note-themes/notepatch-paper-v2.css")
    assert response.status_code == 200
    assert ".np-layout-grid" in response.text
    cleaned = sanitize_note_html(
        '<article class="np-note np-layout-grid" style="color:red" onclick="bad()">'
        '<section class="np-section-card"><span class="np-keyword">Key</span></section>'
        '<img src="https://example.com/source.png"><script>bad()</script></article>'
    )
    assert "np-layout-grid" in cleaned
    assert "np-section-card" in cleaned
    assert "style=" not in cleaned
    assert "onclick=" not in cleaned
    assert "<img" not in cleaned
    assert "<script" not in cleaned

    note = StudyNoteVersionRead.model_validate(
        {
            "id": "note-id",
            "workspace_id": "workspace-id",
            "learning_unit_id": "unit-id",
            "version_no": 1,
            "title": "Visual note",
            "html_object_key": "note.html",
            "json_object_key": "note.json",
            "metadata": {"theme_id": "notepatch-paper-v2"},
            "created_at": "2026-08-21T00:00:00Z",
        }
    )
    assert note.rendering.theme_id == "notepatch-paper-v2"
    assert note.rendering.css_url.endswith("/api/v1/assets/note-themes/notepatch-paper-v2.css")
