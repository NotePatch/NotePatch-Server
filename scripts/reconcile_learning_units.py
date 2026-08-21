#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict

from sqlalchemy import func, select

from notepatch.modules.learning.models.knowledge import KnowledgeChunk
from notepatch.modules.learning.models.learning import LearningUnit, LearningUnitDocument
from notepatch.modules.tasks.models.task import Task
from notepatch.modules.tasks.services.task import TaskService
from notepatch.platform.config import get_settings
from notepatch.platform.database import SessionLocal


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Find and enqueue high-confidence learning-unit merges.")
    parser.add_argument("--workspace-id")
    parser.add_argument("--apply", action="store_true", help="Create merge tasks; default is dry-run.")
    parser.add_argument("--threshold", type=float, default=settings.learning_unit_historical_merge_threshold)
    parser.add_argument("--min-margin", type=float, default=settings.learning_unit_historical_merge_min_margin)
    return parser.parse_args()


def normalized(value: str | None) -> str:
    return "".join(character.lower() for character in (value or "") if character.isalnum())


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    return [sum(values) / len(vectors) for values in zip(*vectors, strict=True)]


def compatible(left: LearningUnit, right: LearningUnit) -> bool:
    return not (
        left.subject
        and right.subject
        and normalized(left.subject) != normalized(right.subject)
        or left.grade_level
        and right.grade_level
        and normalized(left.grade_level) != normalized(right.grade_level)
    )


def is_automatic(unit: LearningUnit) -> bool:
    return (unit.metadata_ or {}).get("source") == "automatic_pipeline"


def is_merge_blocked(unit: LearningUnit) -> bool:
    return unit.merge_status in {"merging", "rebuilding", "failed"} or bool(
        (unit.metadata_ or {}).get("auto_merge_locked")
    )


def pair_score(left: LearningUnit, right: LearningUnit, vectors: dict[str, list[float]]) -> float:
    title_match = normalized(left.title) and normalized(left.title) == normalized(right.title)
    topic_match = normalized(left.topic) and normalized(left.topic) == normalized(right.topic)
    if title_match and compatible(left, right):
        return 1.0
    if topic_match and compatible(left, right):
        return 0.98
    return cosine(vectors.get(left.id, []), vectors.get(right.id, []))


def main() -> int:
    args = parse_args()
    with SessionLocal() as db:
        query = select(LearningUnit).where(LearningUnit.merged_into_id.is_(None))
        if args.workspace_id:
            query = query.where(LearningUnit.workspace_id == args.workspace_id)
        units = db.scalars(query).all()
        by_workspace: dict[str, list[LearningUnit]] = defaultdict(list)
        for unit in units:
            by_workspace[unit.workspace_id].append(unit)

        document_counts = {
            unit_id: int(count)
            for unit_id, count in db.execute(
                select(LearningUnitDocument.learning_unit_id, func.count())
                .group_by(LearningUnitDocument.learning_unit_id)
            ).all()
        }
        chunks = db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.embedding.is_not(None))).all()
        raw_vectors: dict[str, list[list[float]]] = defaultdict(list)
        for chunk in chunks:
            unit_id = (chunk.metadata_ or {}).get("learning_unit_id")
            if isinstance(unit_id, str) and chunk.embedding is not None:
                raw_vectors[unit_id].append(list(chunk.embedding))
        vectors = {unit_id: centroid(items) for unit_id, items in raw_vectors.items()}

        active_tasks = db.scalars(
            select(Task).where(Task.status.in_(("queued", "running")))
        ).all()
        busy_units = {
            value
            for task in active_tasks
            for value in (task.resource_id, (task.payload or {}).get("learning_unit_id"))
            if isinstance(value, str)
        }

        proposals: list[dict] = []
        for workspace_id, workspace_units in by_workspace.items():
            graph: dict[str, set[str]] = defaultdict(set)
            by_id = {unit.id: unit for unit in workspace_units}
            for source in workspace_units:
                if not is_automatic(source) or source.id in busy_units or is_merge_blocked(source):
                    continue
                ranked = sorted(
                    (
                        (pair_score(source, candidate, vectors), candidate)
                        for candidate in workspace_units
                        if candidate.id != source.id
                        and candidate.id not in busy_units
                        and not is_merge_blocked(candidate)
                        and compatible(source, candidate)
                    ),
                    key=lambda item: item[0],
                    reverse=True,
                )
                if not ranked:
                    continue
                best_score, best = ranked[0]
                runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
                if best_score < args.threshold or best_score - runner_up < args.min_margin:
                    continue
                graph[source.id].add(best.id)
                graph[best.id].add(source.id)

            visited: set[str] = set()
            for start in graph:
                if start in visited:
                    continue
                stack = [start]
                component: set[str] = set()
                while stack:
                    current = stack.pop()
                    if current in component:
                        continue
                    component.add(current)
                    stack.extend(graph[current])
                visited.update(component)
                manual = [by_id[item] for item in component if not is_automatic(by_id[item])]
                if len(manual) > 1:
                    continue
                candidates = manual or [by_id[item] for item in component]
                target = sorted(
                    candidates,
                    key=lambda unit: (
                        -document_counts.get(unit.id, 0),
                        unit.created_at,
                        unit.id,
                    ),
                )[0]
                sources = [
                    by_id[item]
                    for item in component
                    if item != target.id and is_automatic(by_id[item])
                ]
                if not sources:
                    continue
                confidence = min(
                    max(pair_score(source, target, vectors), 0.0)
                    for source in sources
                )
                proposals.append(
                    {
                        "workspace_id": workspace_id,
                        "target_learning_unit_id": target.id,
                        "target_title": target.title,
                        "source_learning_unit_ids": sorted(source.id for source in sources),
                        "source_titles": sorted(source.title for source in sources),
                        "confidence": round(confidence, 6),
                        "document_count": sum(document_counts.get(item, 0) for item in component),
                    }
                )

        created = []
        if args.apply:
            service = TaskService(db)
            for proposal in proposals:
                active = service.find_active_task(
                    workspace_id=proposal["workspace_id"],
                    task_type="merge_learning_units",
                    resource_type="learning_unit",
                    resource_id=proposal["target_learning_unit_id"],
                )
                if active is not None:
                    proposal["task_id"] = active.id
                    proposal["task_status"] = active.status
                    continue
                task = service.create_task(
                    workspace_id=proposal["workspace_id"],
                    task_type="merge_learning_units",
                    resource_type="learning_unit",
                    resource_id=proposal["target_learning_unit_id"],
                    payload={
                        "target_learning_unit_id": proposal["target_learning_unit_id"],
                        "source_learning_unit_ids": proposal["source_learning_unit_ids"],
                        "reconciliation": {
                            "automatic": True,
                            "confidence": proposal["confidence"],
                            "threshold": args.threshold,
                            "min_margin": args.min_margin,
                        },
                    },
                )
                proposal["task_id"] = task.id
                proposal["task_status"] = task.status
                created.append(task.id)

        print(
            json.dumps(
                {
                    "mode": "apply" if args.apply else "dry-run",
                    "threshold": args.threshold,
                    "min_margin": args.min_margin,
                    "proposals": proposals,
                    "tasks_created": created,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
