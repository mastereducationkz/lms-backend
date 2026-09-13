"""Visibility rules for task-level homework answer keys.

The task JSON is the authoring source of truth.  This module is the sole seam
that turns teacher-authored keys into the subset a student may receive.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


RELEASE_POLICIES = {"immediate", "after_submission", "after_due_date", "manual"}


def strip_answer_keys(content: dict[str, Any]) -> dict[str, Any]:
    """Return a student-safe copy with task answer keys removed."""
    clean = content.copy()
    clean.pop("answer_keys", None)
    if isinstance(clean.get("tasks"), list):
        clean["tasks"] = [
            {**task, "answer_keys": None} if isinstance(task, dict) else task
            for task in clean["tasks"]
        ]
        for task in clean["tasks"]:
            if isinstance(task, dict):
                task.pop("answer_keys", None)
    return clean


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def student_visible_answer_keys(
    task: dict[str, Any], *, submitted: bool, due_date: datetime | None,
    manually_released_key_ids: Iterable[str] = (), now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return only keys that this student may open, preserving author order."""
    released = set(manually_released_key_ids)
    current = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    deadline = _naive_utc(due_date)
    visible: list[dict[str, Any]] = []
    for raw_key in task.get("answer_keys", []):
        if not isinstance(raw_key, dict):
            continue
        policy = raw_key.get("release_policy", "after_submission")
        key_id = raw_key.get("id")
        allowed = (
            policy == "immediate"
            or (policy == "after_submission" and submitted)
            or (policy == "after_due_date" and deadline is not None and current >= deadline)
            or (policy == "manual" and key_id in released)
        )
        if allowed:
            visible.append(raw_key)
    return visible
