from sqlalchemy import select

from notepatch.modules.learning.models.homework import GradingResult
from notepatch.modules.tasks.services.executor import process_task
from tests.conftest import auth_headers, first_workspace_id, register_user
from tests.test_doctr_worker import FailingDocTrClient
from tests.test_learning_workflow import _create_and_complete_document


def test_rubric_text_produces_official_grading(client, db_sessionmaker, fake_storage):
    user = register_user(client, "official-grading@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    upload = _create_and_complete_document(
        client,
        fake_storage,
        user["access_token"],
        workspace_id,
        filename="official-homework.png",
        document_kind="homework",
    )
    document_id = upload["document"]["id"]
    process_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}/process",
        headers=auth_headers(user["access_token"]),
        json={"options": {"auto_learning": False}},
    )
    assert process_response.status_code == 201
    with db_sessionmaker() as db:
        process_task(
            db,
            process_response.json()["id"],
            storage=fake_storage,
            doctr_client=FailingDocTrClient(),
        )

    homework_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks",
        headers=auth_headers(user["access_token"]),
        json={
            "title": "Official Homework",
            "document_id": document_id,
            "rubric_text": "10 points total; award 8 points for a correct method.",
            "max_score": 10,
        },
    )
    assert homework_response.status_code == 201, homework_response.text
    homework_id = homework_response.json()["id"]
    grade_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grade",
        headers=auth_headers(user["access_token"]),
        json={},
    )
    assert grade_response.status_code == 201
    with db_sessionmaker() as db:
        task = process_task(db, grade_response.json()["id"], storage=fake_storage)
        assert task.status == "succeeded"
        result = db.scalar(
            select(GradingResult).where(
                GradingResult.workspace_id == workspace_id,
                GradingResult.homework_id == homework_id,
            )
        )
        assert result is not None
        assert result.grading_mode == "official"
        assert result.max_score == 10
        assert 0 <= result.confidence <= 1
