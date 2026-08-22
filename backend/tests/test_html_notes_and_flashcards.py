from datetime import timedelta

from sqlalchemy import select

from notepatch.modules.learning.models.learning import (
    Flashcard,
    FlashcardDeck,
    KnowledgePoint,
    KnowledgePointAttempt,
    LearningUnit,
    StudyNoteVersion,
)
from notepatch.modules.learning.services.flashcard_priority import FlashcardPriorityService
from notepatch.modules.learning.services.html_notes import sanitize_note_html
from notepatch.modules.learning.services.workflow import LearningWorkflowService
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow
from tests.conftest import FakeRedis, auth_headers, first_workspace_id, register_user


def _seed_note(db, workspace_id: str, title: str = "Unit"):
    unit = LearningUnit(workspace_id=workspace_id, title=title)
    db.add(unit)
    db.flush()
    point = KnowledgePoint(
        workspace_id=workspace_id,
        learning_unit_id=unit.id,
        name="Linear functions",
        normalized_name="linearfunctions",
        source_document_ids=[],
        metadata_={},
    )
    db.add(point)
    db.flush()
    note = StudyNoteVersion(
        workspace_id=workspace_id,
        learning_unit_id=unit.id,
        version_no=1,
        title="HTML note",
        html_object_key="note.html",
        json_object_key="note.json",
        knowledge_point_ids=[point.id],
        source_document_ids=[],
        source_mistake_ids=[],
        metadata_={},
    )
    db.add(note)
    db.commit()
    return unit, point, note


def test_note_html_sanitizer_preserves_theme_and_removes_active_content():
    cleaned = sanitize_note_html(
        '<section class="np-note-section evil" data-knowledge-point-id="kp-1" onclick="bad()">'
        '<script>alert(1)</script><iframe src="https://bad.example"></iframe>'
        '<mark class="np-highlight np-highlight--red">重点</mark></section>'
    )
    assert "script" not in cleaned
    assert "iframe" not in cleaned
    assert "onclick" not in cleaned
    assert "evil" not in cleaned
    assert 'class="np-note-section"' in cleaned
    assert "np-highlight--red" in cleaned
    assert 'data-knowledge-point-id="kp-1"' in cleaned


def test_note_html_sanitizer_preserves_supported_font_size_classes_only():
    cleaned = sanitize_note_html(
        '<p><span class="np-font-size-24" style="font-size:40px">Large</span>'
        '<span class="np-font-size-18">Unsupported</span></p>'
    )
    assert 'class="np-font-size-24"' in cleaned
    assert "style=" not in cleaned
    assert "np-font-size-18" not in cleaned
    assert "Large" in cleaned
    assert "Unsupported" in cleaned


def test_flashcard_priority_rewards_recent_errors_and_recent_correct_streak_reduces_weight(db_sessionmaker):
    now = utcnow()
    with db_sessionmaker() as db:
        unit, point, note = _seed_note(db, "workspace-priority")
        for age in (60, 45, 30, 15):
            db.add(
                KnowledgePointAttempt(
                    workspace_id=unit.workspace_id,
                    learning_unit_id=unit.id,
                    knowledge_point_id=point.id,
                    outcome="incorrect",
                    score_ratio=0.0,
                    occurred_at=now - timedelta(days=age),
                    metadata_={},
                )
            )
        db.commit()
        before = FlashcardPriorityService(db).calculate(
            workspace_id=unit.workspace_id, learning_unit_id=unit.id, note=note, now=now
        )[0]
        for age in (3, 2, 1):
            db.add(
                KnowledgePointAttempt(
                    workspace_id=unit.workspace_id,
                    learning_unit_id=unit.id,
                    knowledge_point_id=point.id,
                    outcome="correct",
                    score_ratio=1.0,
                    occurred_at=now - timedelta(days=age),
                    metadata_={},
                )
            )
        db.commit()
        after = FlashcardPriorityService(db).calculate(
            workspace_id=unit.workspace_id, learning_unit_id=unit.id, note=note, now=now
        )[0]
        assert after["priority_score"] < before["priority_score"]
        assert after["priority_factors"]["recent_correct_streak"] == 3
        assert after["priority_factors"]["attempt_count"] == 7
        assert after["priority_factors"]["recent_error_count_30d"] == 2
        assert after["priority_factors"]["recent_correct_count_14d"] == 3
        assert after["priority_factors"]["latest_outcome"] == "correct"
        assert after["priority_factors"]["latest_attempt_at"]

        db.add(
            KnowledgePointAttempt(
                workspace_id=unit.workspace_id,
                learning_unit_id=unit.id,
                knowledge_point_id=point.id,
                outcome="incorrect",
                score_ratio=0.0,
                occurred_at=now,
                metadata_={},
            )
        )
        db.commit()
        latest_error = FlashcardPriorityService(db).calculate(
            workspace_id=unit.workspace_id, learning_unit_id=unit.id, note=note, now=now
        )[0]
        assert latest_error["priority_score"] > after["priority_score"]
        assert latest_error["priority_factors"]["recent_correct_streak"] == 0




def test_partial_attempt_counts_as_error_pressure_and_recent_miss(db_sessionmaker):
    now = utcnow()
    with db_sessionmaker() as db:
        unit, point, note = _seed_note(db, "workspace-partial")
        db.add(
            KnowledgePointAttempt(
                workspace_id=unit.workspace_id,
                learning_unit_id=unit.id,
                knowledge_point_id=point.id,
                outcome="partial",
                score_ratio=0.5,
                occurred_at=now,
                metadata_={},
            )
        )
        db.commit()
        result = FlashcardPriorityService(db).calculate(
            workspace_id=unit.workspace_id,
            learning_unit_id=unit.id,
            note=note,
            now=now,
        )[0]
        factors = result["priority_factors"]
        assert factors["error_pressure"] == 0.5
        assert factors["success_pressure"] == 0.5
        assert factors["historical_error_count"] == 1
        assert factors["recent_error_count_30d"] == 1
        assert factors["latest_outcome"] == "partial"


def test_attempt_revision_refreshes_under_lock(db_sessionmaker):
    with db_sessionmaker() as make_db:
        unit = LearningUnit(workspace_id="attempt-lock-workspace", title="Concurrent grading")
        make_db.add(unit)
        make_db.commit()
        unit_id = unit.id

    first = db_sessionmaker()
    second = db_sessionmaker()
    try:
        stale_unit = first.get(LearningUnit, unit_id)
        concurrent_unit = second.get(LearningUnit, unit_id)
        concurrent_unit.attempt_revision = 1
        second.commit()

        refreshed = LearningWorkflowService(first)._increment_attempt_revision(stale_unit)
        first.commit()
        assert refreshed.attempt_revision == 2
    finally:
        first.close()
        second.close()


def test_study_note_debounce_reuses_and_reschedules_one_task(client, db_sessionmaker, monkeypatch):
    monkeypatch.setattr(get_settings(), "study_note_debounce_seconds", 300)
    user = register_user(client, "note-debounce@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    fake_redis = FakeRedis()
    monkeypatch.setattr("notepatch.modules.tasks.services.task.redis.from_url", lambda *_args, **_kwargs: fake_redis)
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="Debounced", knowledge_revision=1)
        db.add(unit)
        db.commit()
        workflow = LearningWorkflowService(db)
        first = workflow.schedule_study_notes(unit, reason="first_document")
        first_due = first.next_attempt_at
        unit.knowledge_revision = 2
        db.commit()
        second = workflow.schedule_study_notes(unit, reason="second_document")
        assert second.id == first.id
        assert second.payload["expected_knowledge_revision"] == 2
        assert second.next_attempt_at >= first_due
        assert not any(fake_redis.lists.values())
        assert sum(len(items) for items in fake_redis.zsets.values()) == 1


def test_study_note_generation_is_queued_immediately_by_default(
    client, db_sessionmaker, monkeypatch
):
    monkeypatch.setattr(get_settings(), "study_note_debounce_seconds", 0)
    user = register_user(client, "note-immediate@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *_args, **_kwargs: fake_redis,
    )
    with db_sessionmaker() as db:
        unit = LearningUnit(workspace_id=workspace_id, title="Immediate", knowledge_revision=1)
        db.add(unit)
        db.commit()

        task = LearningWorkflowService(db).schedule_study_notes(
            unit, reason="knowledge_base_completed"
        )

        assert task.status == "queued"
        assert task.next_attempt_at is None
        assert unit.note_generation_due_at is None
        assert task.id in next(iter(fake_redis.lists.values()))
        assert not any(fake_redis.zsets.values())




def test_flashcard_scheduling_reuses_same_source_revision(
    client, db_sessionmaker, monkeypatch
):
    user = register_user(client, "flashcard-schedule-dedupe@example.com")
    workspace_id = first_workspace_id(client, user["access_token"])
    fake_redis = FakeRedis()
    monkeypatch.setattr(
        "notepatch.modules.tasks.services.task.redis.from_url",
        lambda *_args, **_kwargs: fake_redis,
    )
    with db_sessionmaker() as db:
        unit, _point, note = _seed_note(db, workspace_id, "Deduplicated cards")
        workflow = LearningWorkflowService(db)
        first = workflow.schedule_flashcards(unit, note, reason="study_note_generated")
        second = workflow.schedule_flashcards(unit, note, reason="homework_graded")
        assert second.id == first.id
        assert len(
            db.scalars(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.task_type == "generate_flashcards",
                    Task.resource_id == unit.id,
                )
            ).all()
        ) == 1


def test_flashcard_deck_apis_are_workspace_scoped(client, db_sessionmaker):
    alice = register_user(client, "flashcard-api-a@example.com")
    bob = register_user(client, "flashcard-api-b@example.com")
    alice_workspace = first_workspace_id(client, alice["access_token"])
    bob_workspace = first_workspace_id(client, bob["access_token"])
    with db_sessionmaker() as db:
        unit, point, note = _seed_note(db, alice_workspace, "Private cards")
        deck = FlashcardDeck(
            workspace_id=alice_workspace,
            learning_unit_id=unit.id,
            study_note_version_id=note.id,
            version_no=1,
            attempt_revision=0,
            weighting_config={"error_half_life_days": 30},
            metadata_={},
        )
        db.add(deck)
        db.flush()
        db.add(
            Flashcard(
                workspace_id=alice_workspace,
                deck_id=deck.id,
                knowledge_point_id=point.id,
                front="What is a linear function?",
                back="A function with a constant rate of change.",
                priority_score=1.0,
                priority_factors={"base": 1.0},
                source_refs=[],
                rank=1,
            )
        )
        db.commit()
        unit_id = unit.id
        deck_id = deck.id

    latest = client.get(
        f"/api/v1/workspaces/{alice_workspace}/learning-units/{unit_id}/flashcard-decks/latest",
        headers=auth_headers(alice["access_token"]),
    )
    assert latest.status_code == 200, latest.text
    assert latest.json()["deck"]["id"] == deck_id
    assert latest.json()["cards"][0]["priority_factors"]["base"] == 1.0
    assert latest.json()["cards"][0]["review_hint"] == {
        "primary": {
            "code": "from_notes",
            "message_key": "flashcards.hints.from_notes",
            "tone": "neutral",
            "params": {},
        },
        "badges": [],
        "data_quality": "legacy",
    }
    assert client.get(
        f"/api/v1/workspaces/{bob_workspace}/learning-units/{unit_id}/flashcard-decks/{deck_id}",
        headers=auth_headers(bob["access_token"]),
    ).status_code == 404
