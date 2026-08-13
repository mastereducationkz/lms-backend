"""The one curator task a freeze produces, and everything that can happen to it afterwards.

The CRM owns the freeze lifecycle; the LMS owns the curator task list. This module is the
LMS half of that seam, and it exists as an *upsert keyed on ``source_key``* rather than a
create endpoint because the caller is a scheduler that can run in several workers at once and
may retry after a network failure. "Create unless it already exists" implemented by the
caller is a race; a unique key the database enforces is not.

Four things can happen to a freeze after the task is made, and the task has to follow all of
them without ever becoming a second task:

* **the planned return moves** — the same task is rescheduled. Its identity, its history and
  its in-progress state survive, because a curator who has started work has not stopped
  merely because the date changed;
* **the return is confirmed** — the task completes itself, with a result saying so;
* **the freeze is cancelled** — the task is superseded, not deleted;
* **nothing** — it simply falls due, and the existing Tasks UI marks it overdue on its own.

What this module deliberately does *not* do is resume a student. A planned date arriving is
not a fact about the student; a curator confirming they came back is. That is why the freeze
produces a task at all instead of a state transition.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: The template every freeze-return task hangs off. Created on first use rather than seeded
#: by a migration, so a fresh environment needs no manual step.
TEMPLATE_TASK_TYPE = "freeze_return"
TEMPLATE_TITLE = "Возвращение из заморозки"
#: Shown as the task's category in the curator Tasks UI.
TEMPLATE_CATEGORY = "Заморозка"

#: Statuses a task can be in and still be somebody's open work.
OPEN_STATUSES = ("pending", "in_progress")

#: Written when the curator confirms the student is back.
RESULT_RETURN_CONFIRMED = "Возвращение подтверждено"
RESULT_FREEZE_CANCELLED = "Заморозка отменена"


def source_key_for(freeze_period_id: int) -> str:
    """``freeze_return:{id}`` — deterministic, so a retry cannot make a second task."""
    return f"{TEMPLATE_TASK_TYPE}:{int(freeze_period_id)}"


def _template(db: Session):
    """The shared template, created once. Concurrency-safe: a lost insert re-reads."""
    from src.curator.models import CuratorTaskTemplate

    existing = (
        db.query(CuratorTaskTemplate)
        .filter(CuratorTaskTemplate.task_type == TEMPLATE_TASK_TYPE)
        .first()
    )
    if existing is not None:
        return existing
    template = CuratorTaskTemplate(
        title=TEMPLATE_TITLE,
        description="Связаться с учеником или родителем и подтвердить возвращение",
        task_type=TEMPLATE_TASK_TYPE,
        scope="student",
        is_active=True,
    )
    db.add(template)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return (
            db.query(CuratorTaskTemplate)
            .filter(CuratorTaskTemplate.task_type == TEMPLATE_TASK_TYPE)
            .first()
        )
    return template


def _existing(db: Session, key: str):
    from src.curator.models import CuratorTaskInstance

    return (
        db.query(CuratorTaskInstance)
        .filter(CuratorTaskInstance.source_key == key)
        .first()
    )


def upsert_freeze_return_task(
    db: Session,
    *,
    freeze_period_id: int,
    curator_id: int,
    student_id: Optional[int],
    due_date: datetime,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create the task, or bring the existing one in line with the freeze.

    Returns ``{"task_id", "action"}`` where action is ``created`` | ``rescheduled`` |
    ``unchanged``. Never creates a second task for one freeze period.
    """
    from src.curator.models import CuratorTaskInstance

    key = source_key_for(freeze_period_id)
    task = _existing(db, key)

    if task is None:
        template = _template(db)
        task = CuratorTaskInstance(
            template_id=template.id,
            curator_id=curator_id,
            student_id=student_id,
            status="pending",
            due_date=due_date,
            custom_title=title,
            source_key=key,
        )
        db.add(task)
        try:
            db.flush()
        except IntegrityError:
            # Another worker won the race. Its row is the one that counts.
            db.rollback()
            task = _existing(db, key)
            if task is None:  # pragma: no cover - only if the row vanished mid-race
                raise
            return {"task_id": task.id, "action": "unchanged"}
        return {"task_id": task.id, "action": "created"}

    if task.status in ("completed", "cancelled"):
        # A finished task is history. A later date change does not reopen it.
        return {"task_id": task.id, "action": "unchanged"}

    changed = False
    if due_date is not None and task.due_date != due_date:
        task.due_date = due_date
        changed = True
    if title and task.custom_title != title:
        task.custom_title = title
        changed = True
    if curator_id and task.curator_id != curator_id:
        # The freeze's responsible-curator snapshot moved. Follow it, but do not create a
        # second task — the work is the same work.
        task.curator_id = curator_id
        changed = True
    # `status` is deliberately untouched: a curator who has started work has not stopped
    # because the planned date moved.
    return {"task_id": task.id, "action": "rescheduled" if changed else "unchanged"}


def close_freeze_return_task(
    db: Session, *, freeze_period_id: int, outcome: str
) -> dict[str, Any]:
    """Finish the task when the freeze itself ends.

    ``outcome`` is ``resumed`` (the curator confirmed the return) or ``cancelled``. Both are
    terminal and both preserve the row and its history — nothing is deleted, and a task that
    was already completed is left exactly as the curator left it.
    """
    task = _existing(db, source_key_for(freeze_period_id))
    if task is None:
        return {"task_id": None, "action": "absent"}
    if task.status in ("completed", "cancelled"):
        return {"task_id": task.id, "action": "unchanged"}

    task.status = "completed" if outcome == "resumed" else "cancelled"
    task.completed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    task.result_text = (
        RESULT_RETURN_CONFIRMED if outcome == "resumed" else RESULT_FREEZE_CANCELLED
    )
    return {"task_id": task.id, "action": task.status}
