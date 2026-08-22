from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from notepatch.modules.learning.models.learning import KnowledgePoint, KnowledgePointAttempt, StudyNoteVersion
from notepatch.platform.config import get_settings
from notepatch.platform.database import utcnow


class FlashcardPriorityService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def calculate(
        self,
        *,
        workspace_id: str,
        learning_unit_id: str,
        note: StudyNoteVersion,
        now: datetime | None = None,
    ) -> list[dict]:
        now = now or utcnow()
        points = self.db.scalars(
            select(KnowledgePoint).where(
                KnowledgePoint.workspace_id == workspace_id,
                KnowledgePoint.learning_unit_id == learning_unit_id,
            )
        ).all()
        attempts = self.db.scalars(
            select(KnowledgePointAttempt)
            .where(
                KnowledgePointAttempt.workspace_id == workspace_id,
                KnowledgePointAttempt.learning_unit_id == learning_unit_id,
            )
            .order_by(KnowledgePointAttempt.occurred_at.desc())
        ).all()
        by_point: dict[str, list[KnowledgePointAttempt]] = defaultdict(list)
        for attempt in attempts:
            by_point[attempt.knowledge_point_id].append(attempt)
        note_ids = set(note.knowledge_point_ids or [])
        weighted = [self._score(point, by_point[point.id], point.id in note_ids, now) for point in points]
        weighted = [item for item in weighted if item["in_note"] or item["attempt_count"]]
        weighted.sort(key=lambda item: (-item["priority_score"], item["name"], item["id"]))
        return weighted[: self.settings.flashcard_max_cards]

    def weighting_config(self) -> dict:
        return {
            "error_half_life_days": self.settings.flashcard_error_half_life_days,
            "success_half_life_days": self.settings.flashcard_success_half_life_days,
            "error_multiplier": self.settings.flashcard_error_multiplier,
            "success_multiplier": self.settings.flashcard_success_multiplier,
            "correct_streak_multiplier": self.settings.flashcard_correct_streak_multiplier,
            "max_correct_streak": self.settings.flashcard_max_correct_streak,
            "hint_error_window_days": self.settings.flashcard_hint_error_window_days,
            "hint_success_window_days": self.settings.flashcard_hint_success_window_days,
            "hint_fresh_attempt_days": self.settings.flashcard_hint_fresh_attempt_days,
            "hint_frequent_error_count": self.settings.flashcard_hint_frequent_error_count,
            "hint_improving_streak": self.settings.flashcard_hint_improving_streak,
        }

    def _score(
        self,
        point: KnowledgePoint,
        attempts: list[KnowledgePointAttempt],
        in_note: bool,
        now: datetime,
    ) -> dict:
        error_pressure = 0.0
        success_pressure = 0.0
        recent_error_count = 0
        recent_correct_count = 0
        historical_error_count = 0
        latest_error_at = None
        latest_correct_at = None
        for attempt in attempts:
            occurred_at = attempt.occurred_at
            if occurred_at.tzinfo is None:
                occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            age_days = max((now - occurred_at).total_seconds() / 86400, 0.0)
            error_pressure += (1.0 - attempt.score_ratio) * math.pow(
                2.0, -age_days / self.settings.flashcard_error_half_life_days
            )
            success_pressure += attempt.score_ratio * math.pow(
                2.0, -age_days / self.settings.flashcard_success_half_life_days
            )
            if attempt.outcome in {"incorrect", "partial"}:
                historical_error_count += 1
                latest_error_at = latest_error_at or occurred_at
                if age_days <= self.settings.flashcard_hint_error_window_days:
                    recent_error_count += 1
            if attempt.outcome == "correct":
                latest_correct_at = latest_correct_at or occurred_at
                if age_days <= self.settings.flashcard_hint_success_window_days:
                    recent_correct_count += 1
        correct_streak = 0
        for attempt in attempts:
            if attempt.outcome != "correct":
                break
            correct_streak += 1
            if correct_streak >= self.settings.flashcard_max_correct_streak:
                break
        base = 1.0 if in_note else 0.4
        priority = (base + self.settings.flashcard_error_multiplier * error_pressure) / (
            1.0 + self.settings.flashcard_success_multiplier * success_pressure
        )
        priority *= math.pow(self.settings.flashcard_correct_streak_multiplier, correct_streak)
        priority = round(min(max(priority, 0.1), 10.0), 4)
        if priority >= self.settings.note_highlight_red_threshold:
            highlight_level = "red"
        elif priority >= self.settings.note_highlight_yellow_threshold:
            highlight_level = "yellow"
        else:
            highlight_level = None
        return {
            "id": point.id,
            "name": point.name,
            "priority_score": priority,
            "highlight_level": highlight_level,
            "in_note": in_note,
            "attempt_count": len(attempts),
            "source_document_ids": point.source_document_ids or [],
            "priority_factors": {
                "hint_data_version": 1,
                "snapshot_at": _iso(now),
                "base": base,
                "in_note": in_note,
                "error_pressure": round(error_pressure, 6),
                "success_pressure": round(success_pressure, 6),
                "attempt_count": len(attempts),
                "historical_error_count": historical_error_count,
                "recent_error_count_30d": recent_error_count,
                "recent_correct_count_14d": recent_correct_count,
                "recent_correct_streak": correct_streak,
                "latest_outcome": attempts[0].outcome if attempts else None,
                "latest_attempt_at": _iso(attempts[0].occurred_at) if attempts else None,
                "latest_error_at": _iso(latest_error_at),
                "latest_correct_at": _iso(latest_correct_at),
            },
        }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
