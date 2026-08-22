from datetime import datetime, timedelta, timezone

import pytest

from notepatch.modules.learning.services.flashcard_hints import build_flashcard_review_hint


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _factors(**overrides):
    factors = {
        "hint_data_version": 1,
        "snapshot_at": NOW.isoformat(),
        "base": 1.0,
        "in_note": True,
        "attempt_count": 1,
        "historical_error_count": 0,
        "recent_error_count_30d": 0,
        "recent_correct_count_14d": 0,
        "recent_correct_streak": 0,
        "latest_outcome": "correct",
        "latest_attempt_at": (NOW - timedelta(days=1)).isoformat(),
    }
    factors.update(overrides)
    return factors


@pytest.mark.parametrize(
    ("factors", "expected"),
    [
        (
            _factors(
                recent_correct_streak=3,
                historical_error_count=4,
                recent_error_count_30d=2,
                latest_outcome="correct",
            ),
            "recently_improving",
        ),
        (
            _factors(
                latest_outcome="partial",
                historical_error_count=1,
                recent_error_count_30d=1,
            ),
            "just_missed",
        ),
        (
            _factors(
                latest_outcome="incorrect",
                latest_attempt_at=(NOW - timedelta(days=10)).isoformat(),
                historical_error_count=3,
                recent_error_count_30d=2,
            ),
            "frequent_recent_errors",
        ),
        (_factors(recent_correct_streak=1), "recently_correct"),
        (
            _factors(
                attempt_count=0,
                latest_outcome=None,
                latest_attempt_at=None,
            ),
            "from_notes",
        ),
        (
            _factors(
                latest_outcome="incorrect",
                latest_attempt_at=(NOW - timedelta(days=45)).isoformat(),
                historical_error_count=3,
            ),
            "historical_review",
        ),
    ],
)
def test_review_hint_primary_precedence(factors, expected):
    hint = build_flashcard_review_hint(factors, now=NOW)
    assert hint["primary"]["code"] == expected
    assert hint["primary"]["message_key"] == f"flashcards.hints.{expected}"
    assert hint["data_quality"] == "complete"
    assert len(hint["badges"]) <= 3


def test_review_hint_badges_explain_streak_errors_and_latest_outcome():
    hint = build_flashcard_review_hint(
        _factors(
            recent_correct_streak=3,
            historical_error_count=4,
            recent_error_count_30d=2,
            latest_outcome="correct",
        ),
        now=NOW,
    )
    badges = {badge["code"]: badge for badge in hint["badges"]}
    assert badges["correct_streak"]["params"] == {"correct_streak": 3}
    assert badges["recent_errors"]["params"] == {"count": 2, "window_days": 30}
    assert badges["latest_outcome"]["params"] == {"outcome": "correct"}


def test_legacy_hint_uses_only_frozen_priority_factors():
    factors = {"base": 1.0, "error_pressure": 2.4, "recent_correct_streak": 0}
    old = build_flashcard_review_hint(factors, now=NOW)
    much_later = build_flashcard_review_hint(factors, now=NOW + timedelta(days=365))
    assert old == much_later
    assert old["primary"]["code"] == "historical_review"
    assert old["data_quality"] == "legacy"
