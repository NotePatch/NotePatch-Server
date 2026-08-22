import json
from pathlib import Path

import pytest

from notepatch.modules.tasks.models.task import Task
from notepatch.modules.ai.services.skill_runner import OpenClawSkillOutputError, OpenClawSkillRunner
from notepatch.modules.learning.schemas.skills import QuestionExtractionResult


class RuntimeStub:
    def __init__(self, root: Path) -> None:
        self.root = root

    def sync_workspace_documents(
        self, *, db, storage, workspace_id, task_id, model_ids=None
    ):
        task_root = self.root / task_id
        input_dir = task_root / "input"
        output_dir = task_root / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "gateway_url": "http://gateway:18789",
            "gateway_token": "token",
            "container_name": "gateway",
            "workspace_dir": str(self.root),
            "documents_index_path": "/workspace/notepatch/documents/index.json",
            "documents_root_path": "/workspace/notepatch/documents",
            "task_output_path": f"/workspace/notepatch/openclaw/tasks/{task_id}/output",
            "host_task_input_dir": str(input_dir),
            "host_task_output_dir": str(output_dir),
        }


class CorrectingRunner:
    def __init__(self, always_invalid: bool = False) -> None:
        self.calls = []
        self.always_invalid = always_invalid

    def prepare_task_dir(self, workspace_id, task_id):
        return Path("/tmp")

    def run_task(self, workspace_id, task_id, payload):
        self.calls.append(payload)
        output = Path(payload["_openclaw"]["host_task_output_dir"]) / "questions.json"
        if len(self.calls) == 1 or self.always_invalid:
            output.write_text("{}", encoding="utf-8")
        else:
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

    def collect_output(self, workspace_id, task_id):
        return {}

    def cleanup(self, workspace_id, task_id):
        return None


class DomainCorrectingRunner(CorrectingRunner):
    def run_task(self, workspace_id, task_id, payload):
        self.calls.append(payload)
        output = Path(payload["_openclaw"]["host_task_output_dir"]) / "questions.json"
        answer = "5" if len(self.calls) == 1 else "4"
        output.write_text(
            json.dumps(
                {
                    "questions": [
                        {
                            "sequence_no": 1,
                            "question_type": "short_answer",
                            "prompt": "What is 2 + 2?",
                            "answer": answer,
                            "page_refs": [0],
                            "evidence": f"2 + 2 = {answer}",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return {"runner": "test", "answer": "done"}


def _task() -> Task:
    return Task(
        id="task-1",
        workspace_id="workspace-1",
        task_type="extract_questions",
        payload={"ai_model": "openai/gpt-5.4"},
    )


def test_skill_runner_corrects_invalid_output_in_same_session(db_sessionmaker, fake_storage, tmp_path):
    gateway = CorrectingRunner()
    with db_sessionmaker() as db:
        result, metadata = OpenClawSkillRunner(
            db=db,
            storage=fake_storage,
            gateway_runner=gateway,
            runtime_service=RuntimeStub(tmp_path),
        ).execute(
            task=_task(),
            skill_name="notepatch_question_extractor",
            input_payload={"ocr_text": "2 + 2 = 4"},
            output_filename="questions.json",
            schema=QuestionExtractionResult,
        )
    assert result.questions[0].answer == "4"
    assert len(gateway.calls) == 2
    assert {call["_openclaw"]["session_key"] for call in gateway.calls} == {metadata["session_key"]}
    assert {call["ai_model"] for call in gateway.calls} == {"openai/gpt-5.4"}
    assert metadata["provider_model"] == "openai/gpt-5.4"
    skill_input = json.loads((tmp_path / "task-1" / "input" / "input.json").read_text(encoding="utf-8"))
    assert skill_input["_output_contract"]["filename"] == "questions.json"
    assert skill_input["_output_contract"]["json_schema"]["required"] == ["questions"]


def test_skill_runner_corrects_domain_invalid_output_in_same_session(
    db_sessionmaker, fake_storage, tmp_path
):
    gateway = DomainCorrectingRunner()

    def validate_answer(result: QuestionExtractionResult) -> None:
        if result.questions[0].answer != "4":
            raise ValueError("answer must be supported by the supplied evidence")

    with db_sessionmaker() as db:
        result, _ = OpenClawSkillRunner(
            db=db,
            storage=fake_storage,
            gateway_runner=gateway,
            runtime_service=RuntimeStub(tmp_path),
        ).execute(
            task=_task(),
            skill_name="notepatch_question_extractor",
            input_payload={"ocr_text": "2 + 2 = 4"},
            output_filename="questions.json",
            schema=QuestionExtractionResult,
            output_validator=validate_answer,
        )

    assert result.questions[0].answer == "4"
    assert len(gateway.calls) == 2
    assert "answer must be supported" in gateway.calls[1]["prompt"]


def test_skill_runner_rejects_persistently_invalid_output(db_sessionmaker, fake_storage, tmp_path):
    with db_sessionmaker() as db, pytest.raises(OpenClawSkillOutputError):
        OpenClawSkillRunner(
            db=db,
            storage=fake_storage,
            gateway_runner=CorrectingRunner(always_invalid=True),
            runtime_service=RuntimeStub(tmp_path),
        ).execute(
            task=_task(),
            skill_name="notepatch_question_extractor",
            input_payload={"ocr_text": "bad"},
            output_filename="questions.json",
            schema=QuestionExtractionResult,
        )


def test_skill_runner_reuses_valid_output_from_previous_attempt(db_sessionmaker, fake_storage, tmp_path):
    task = _task()
    task.attempt = 2
    output = tmp_path / task.id / "output" / "questions.json"
    output.parent.mkdir(parents=True, exist_ok=True)
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
    gateway = CorrectingRunner()

    with db_sessionmaker() as db:
        result, metadata = OpenClawSkillRunner(
            db=db,
            storage=fake_storage,
            gateway_runner=gateway,
            runtime_service=RuntimeStub(tmp_path),
        ).execute(
            task=task,
            skill_name="notepatch_question_extractor",
            input_payload={"ocr_text": "2 + 2 = 4"},
            output_filename="questions.json",
            schema=QuestionExtractionResult,
        )

    assert result.questions[0].answer == "4"
    assert gateway.calls == []
    assert metadata["run_result"]["reused_output"] is True
