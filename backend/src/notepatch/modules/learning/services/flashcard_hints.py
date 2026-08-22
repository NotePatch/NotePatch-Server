from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from notepatch.platform.config import Settings, get_settings


def build_flashcard_review_hint(
    priority_factors: dict[str, Any] | None,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a stable, localizable explanation from the deck's stored snapshot."""
    factors = priority_factors or {}
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if factors.get("hint_data_version") != 1:
        return _legacy_hint(factors, settings)

    attempt_count = _int(factors.get("attempt_count"))
    recent_errors = _int(factors.get("recent_error_count_30d"))
    streak = _int(factors.get("recent_correct_streak"))
    historical_errors = _int(factors.get("historical_error_count"))
    latest_outcome = _outcome(factors.get("latest_outcome"))
    latest_attempt_at = _datetime(factors.get("latest_attempt_at"))
    snapshot_at = _datetime(factors.get("snapshot_at")) or now
    fresh = _is_recent(latest_attempt_at, settings.flashcard_hint_fresh_attempt_days, snapshot_at)
    in_note = bool(factors.get("in_note", float(factors.get("base", 0.0) or 0.0) >= 1.0))

    if streak >= settings.flashcard_hint_improving_streak:
        primary = _item("recently_improving", "positive", correct_streak=streak)
    elif fresh and latest_outcome in {"incorrect", "partial"}:
        primary = _item("just_missed", "warning", outcome=latest_outcome)
    elif recent_errors >= settings.flashcard_hint_frequent_error_count:
        primary = _item(
            "frequent_recent_errors",
            "warning",
            count=recent_errors,
            window_days=settings.flashcard_hint_error_window_days,
        )
    elif fresh and latest_outcome == "correct":
        primary = _item("recently_correct", "positive")
    elif attempt_count == 0 and in_note:
        primary = _item("from_notes", "neutral")
    elif historical_errors > 0:
        primary = _item("historical_review", "neutral", count=historical_errors)
    else:
        primary = _item("general_review", "neutral")

    badges: list[dict[str, Any]] = []
    if streak:
        badges.append(_badge("correct_streak", "positive", correct_streak=streak))
    if recent_errors:
        badges.append(
            _badge(
                "recent_errors",
                "warning",
                count=recent_errors,
                window_days=settings.flashcard_hint_error_window_days,
            )
        )
    elif historical_errors:
        badges.append(_badge("historical_errors", "neutral", count=historical_errors))
    if latest_outcome:
        tone = "positive" if latest_outcome == "correct" else "warning"
        badges.append(_badge("latest_outcome", tone, outcome=latest_outcome))
    return {"primary": primary, "badges": badges[:3], "data_quality": "complete"}


def _legacy_hint(factors: dict[str, Any], settings: Settings) -> dict[str, Any]:
    streak = _int(factors.get("recent_correct_streak"))
    error_pressure = float(factors.get("error_pressure", 0.0) or 0.0)
    base = float(factors.get("base", 0.0) or 0.0)
    if streak >= settings.flashcard_hint_improving_streak:
        primary = _item("recently_improving", "positive", correct_streak=streak)
    elif error_pressure > 0:
        primary = _item("historical_review", "neutral")
    elif base >= 1.0:
        primary = _item("from_notes", "neutral")
    else:
        primary = _item("general_review", "neutral")
    badges = [_badge("correct_streak", "positive", correct_streak=streak)] if streak else []
    return {"primary": primary, "badges": badges, "data_quality": "legacy"}


def _item(code: str, tone: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "message_key": f"flashcards.hints.{code}", "tone": tone, "params": params}


def _badge(code: str, tone: str, **params: Any) -> dict[str, Any]:
    return {"code": code, "message_key": f"flashcards.badges.{code}", "tone": tone, "params": params}


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _outcome(value: Any) -> str | None:
    return value if value in {"correct", "partial", "incorrect"} else None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_recent(value: datetime | None, days: int, now: datetime) -> bool:
    if value is None:
        return False
    normalized_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = normalized_now.astimezone(timezone.utc) - value.astimezone(timezone.utc)
    return age.total_seconds() >= 0 and age.total_seconds() <= days * 86400
