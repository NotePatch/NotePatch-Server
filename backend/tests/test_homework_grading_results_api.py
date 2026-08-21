from datetime import timedelta

from notepatch.modules.learning.models.homework import GradingResult, Homework
from notepatch.platform.database import utcnow
from tests.conftest import auth_headers, first_workspace_id, register_user


def test_homework_responses_include_latest_grading_result_and_history(
    client,
    db_sessionmaker,
):
    alice = register_user(client, "grading-results-alice@example.com")
    bob = register_user(client, "grading-results-bob@example.com")
    workspace_id = first_workspace_id(client, alice["access_token"])
    bob_workspace_id = first_workspace_id(client, bob["access_token"])

    with db_sessionmaker() as db:
        homework = Homework(
            workspace_id=workspace_id,
            title="Algebra homework",
            status="graded",
            max_score=100,
            metadata_={},
            created_by_user_id=alice["user"]["id"],
        )
        db.add(homework)
        db.flush()
        older = GradingResult(
            workspace_id=workspace_id,
            homework_id=homework.id,
            student_user_id=alice["user"]["id"],
            score=70,
            max_score=100,
            grading_mode="provisional",
            confidence=0.6,
            feedback="First grading",
            created_at=utcnow() - timedelta(minutes=5),
        )
        latest = GradingResult(
            workspace_id=workspace_id,
            homework_id=homework.id,
            student_user_id=alice["user"]["id"],
            score=90,
            max_score=100,
            grading_mode="official",
            confidence=0.95,
            feedback="Latest grading",
            created_at=utcnow(),
        )
        db.add_all([older, latest])
        db.commit()
        homework_id = homework.id
        latest_id = latest.id

    headers = auth_headers(alice["access_token"])
    listing = client.get(f"/api/v1/workspaces/{workspace_id}/homeworks", headers=headers)
    assert listing.status_code == 200
    item = next(row for row in listing.json() if row["id"] == homework_id)
    assert item["latest_grading_result"]["id"] == latest_id
    assert item["latest_grading_result"]["score"] == 90
    assert item["latest_grading_result"]["grading_mode"] == "official"

    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}",
        headers=headers,
    )
    assert detail.status_code == 200
    assert detail.json()["latest_grading_result"]["score"] == 90

    history = client.get(
        f"/api/v1/workspaces/{workspace_id}/homeworks/{homework_id}/grading-results",
        headers=headers,
    )
    assert history.status_code == 200
    assert [row["score"] for row in history.json()] == [90, 70]

    cross_workspace = client.get(
        f"/api/v1/workspaces/{bob_workspace_id}/homeworks/{homework_id}/grading-results",
        headers=auth_headers(bob["access_token"]),
    )
    assert cross_workspace.status_code == 404
