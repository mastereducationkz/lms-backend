"""A frozen student's onboarding card pauses. It does not close.

A freeze deletes the student's membership of the frozen group — that is the point of it, and
it is what stops the school billing for lessons nobody agreed to teach. But the onboarding
reconciler derives the curator↔student relationship *from* membership, so the deletion read
as "this student is no longer yours" and the cycle was closed with ``relationship_ended``.
The curator's notes, their «В работе» status and everything they had learned about the
student went into history, and when the student came back they arrived as a brand-new card
with an empty page, as though nobody had ever onboarded them.

The owner's decision is **pause**: while the student is frozen the card is off the board, its
overdue clock is stopped, and when the freeze ends the same card comes back exactly as the
curator left it.

**Where pause comes from.** :mod:`src.curator.freeze_mirror` — the CRM's freeze decisions as
the LMS records them, one row per (student, group). There is no second source of truth and no
freeze state stored on the onboarding row: :func:`sync_pauses_for_students` re-derives every
open card of a student from the mirror, so it is idempotent, order-tolerant and cannot drift.
It is called from the freeze-state door (``POST /internal/crm/curator/students/freeze-state``)
and the same question is asked again by the reconciler on every sweep.

**The clock.** ``paused_seconds`` accumulates settled pauses; ``paused_at`` marks one still
running. Every elapsed-time rule in :mod:`src.curator.onboarding_core` subtracts the total
(:func:`~src.curator.onboarding_core.paused_seconds_total`), so while a card is paused the
"time since" expressions stand still — the pause grows at exactly the rate wall-clock time
does — and after a resume they carry on from where they stopped. A card frozen on day 1 of
its two-day window comes back with one day left, not overdue by ten.

**The race, and why the close has to be reversible.** The CRM freezes in this order: delete
the LMS membership, commit, call the LMS's ``/reconcile-student`` *synchronously*, and
enqueue the freeze-state mirror on the outbox to be delivered *later*. So at the moment the
LMS is asked to re-derive the cards, it has not yet been told about the freeze — the mirror
still says the student is studying. The reconciler closes the card, and the delivery that
would have paused it arrives seconds afterwards to find nothing open.

Rather than ask the CRM to reorder its saga, this module treats that close as what it is: a
decision taken without the evidence. :func:`sync_pauses_for_students` reverses it, under
conditions narrow enough that nothing else can match — see :func:`_reversible_close`. The
card is restored to the status the ``cycle.closed`` event recorded and then paused, so the
end state is identical whichever order the two messages arrived in.

Nothing else in this codebase un-closes a cycle, and nothing else may.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from src.curator.freeze_mirror import GROUP_WIDE, FreezeIndex, freeze_index
from src.curator.onboarding_core import (
    ACTIVE_STATUSES,
    END_RELATIONSHIP_ENDED,
    END_TRANSFERRED_OUT,
    STATUS_CANCELLED,
    OnboardingActor,
    _as_naive_utc,
    _utcnow,
    active_cycle,
    is_paused,
    paused_seconds_total,
    record_event,
)
from src.schemas.models import CuratorOnboarding, CuratorOnboardingEvent

logger = logging.getLogger(__name__)

EVENT_PAUSED = "cycle.paused"
EVENT_RESUMED = "cycle.resumed"
EVENT_CLOSE_REVERSED = "cycle.close_reversed"
EVENT_CYCLE_CLOSED = "cycle.closed"

#: How recently a close must have happened for a freeze to be able to explain it.
#:
#: Not a business rule: the width of the gap between the CRM's two messages. The reconcile
#: call is synchronous and the mirror delivery rides the outbox, which is normally seconds
#: behind and has been hours behind when the queue backs up. Two days is comfortably wider
#: than any backlog seen in production and far too narrow to reach a close that happened for
#: some other reason weeks ago.
CLOSE_REVERSAL_WINDOW = timedelta(days=2)

#: The only closes a freeze may reverse. Both are the reconciler saying "the membership is
#: gone", which is exactly what a freeze causes and exactly what it explains. A ``completed``
#: cycle (the curator finished it), a ``manual`` one (a person decided) and
#: ``opened_in_error`` (a repair) are all somebody's decision, and a freeze does not overrule
#: a decision.
REVERSIBLE_END_REASONS: tuple[str, ...] = (END_RELATIONSHIP_ENDED, END_TRANSFERRED_OUT)


def freeze_view_for(db: Session, student_ids: Iterable[int]) -> FreezeIndex:
    """The freeze mirror for these students, in one query. The only source of pause state."""
    return freeze_index(db, student_ids)


def card_is_frozen(index: FreezeIndex, row: CuratorOnboarding) -> bool:
    """Does the mirror say this card's enrollment is suspended?

    Scoped, like every other freeze question: a SAT freeze must leave the student's IELTS
    card on its curator's board. A card whose ``group_id`` is NULL — the group was deleted,
    or the cycle was opened without one — is answered only by a **student-wide** freeze:
    there is no enrollment to ask about, and reading "frozen somewhere" as "frozen here"
    would hide an IELTS card because of a SAT freeze, which is the exact bug scoped freezes
    exist to prevent.
    """
    if row.group_id is None:
        return index.is_frozen_now(row.student_id, GROUP_WIDE)
    return index.is_frozen_now(row.student_id, int(row.group_id))


def pause_cycle(
    db: Session,
    row: CuratorOnboarding,
    actor: Optional[OnboardingActor] = None,
) -> bool:
    """Stop this card's clocks and take it off the board. False if it cannot be paused.

    Idempotent: a card already paused stays paused with its existing ``paused_at``, so a
    redelivered freeze does not restart the pause and quietly extend it.
    """
    if row.ended_at is not None or is_paused(row):
        return False
    actor = actor or OnboardingActor.system()
    now = _utcnow()
    row.paused_at = now
    row.updated_at = now
    record_event(
        db,
        row,
        actor,
        EVENT_PAUSED,
        before={"status": row.status, "paused_at": None},
        after={"status": row.status, "paused_at": now.isoformat()},
    )
    return True


def resume_cycle(
    db: Session,
    row: CuratorOnboarding,
    actor: Optional[OnboardingActor] = None,
) -> bool:
    """Start the clocks again, banking the time the card spent paused. False if not paused.

    The status is never touched: coming back means coming back to exactly what the curator
    was doing, which is the whole point of pausing rather than closing.
    """
    if row.ended_at is not None or not is_paused(row):
        return False
    actor = actor or OnboardingActor.system()
    now = _utcnow()
    banked = int(paused_seconds_total(row, now))
    before_paused_at = _as_naive_utc(row.paused_at)
    row.paused_seconds = banked
    row.paused_at = None
    row.updated_at = now
    record_event(
        db,
        row,
        actor,
        EVENT_RESUMED,
        before={
            "paused_at": before_paused_at.isoformat() if before_paused_at else None,
        },
        after={"status": row.status, "paused_seconds": banked},
    )
    return True


def _closed_status_before(db: Session, row: CuratorOnboarding) -> Optional[str]:
    """What this card said before it was closed, according to its own history.

    Read rather than guessed. :func:`~src.curator.onboarding_core.close_cycle` rewrites an
    in-flight status to ``cancelled``, and the only record of what it was is the ``before``
    of the ``cycle.closed`` event it wrote in the same transaction. No event, no reversal —
    restoring a card to a status nobody recorded would be inventing the curator's work.
    """
    event = (
        db.query(CuratorOnboardingEvent)
        .filter(
            CuratorOnboardingEvent.onboarding_id == row.id,
            CuratorOnboardingEvent.action == EVENT_CYCLE_CLOSED,
        )
        .order_by(CuratorOnboardingEvent.created_at.desc(), CuratorOnboardingEvent.id.desc())
        .first()
    )
    if event is None or not isinstance(event.before, dict):
        return None
    status = event.before.get("status")
    return status if isinstance(status, str) and status else None


def _reversible_close(
    db: Session, row: CuratorOnboarding, now: datetime
) -> Optional[str]:
    """The status to restore, or ``None`` when this close must stay closed.

    Every condition is a reason to leave the row alone if it does not hold:

    * the close was the reconciler reacting to a missing membership
      (:data:`REVERSIBLE_END_REASONS`) — anything else is somebody's decision;
    * it happened inside :data:`CLOSE_REVERSAL_WINDOW`, so it can plausibly be *this* freeze;
    * the card was still in flight when it closed (it became ``cancelled``) — a cycle that
      had reached ``done`` finished, and a freeze does not un-finish it;
    * the pair has no open cycle now, because the partial unique index permits exactly one
      and the reversal must never be the write that violates it;
    * the history says what the status was.
    """
    if row.ended_at is None or row.status != STATUS_CANCELLED:
        return None
    if row.end_reason not in REVERSIBLE_END_REASONS:
        return None
    ended_at = _as_naive_utc(row.ended_at)
    if ended_at is None or (now - ended_at) > CLOSE_REVERSAL_WINDOW:
        return None
    if active_cycle(db, int(row.curator_id), int(row.student_id)) is not None:
        return None
    status = _closed_status_before(db, row)
    if status not in ACTIVE_STATUSES:
        return None
    return status


def _reverse_close(
    db: Session, row: CuratorOnboarding, status: str, actor: OnboardingActor
) -> None:
    """Un-close a card the freeze explains, and record that this is what happened."""
    before = {
        "status": row.status,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "end_reason": row.end_reason,
    }
    now = _utcnow()
    row.status = status
    row.ended_at = None
    row.end_reason = None
    row.updated_at = now
    record_event(
        db,
        row,
        actor,
        EVENT_CLOSE_REVERSED,
        before=before,
        after={"status": status, "reason": "student frozen"},
    )
    logger.info(
        "onboarding %s: close reversed, the student was frozen (curator=%s student=%s)",
        row.id,
        row.curator_id,
        row.student_id,
    )


def sync_pauses_for_students(
    db: Session,
    student_ids: Iterable[int],
    actor: Optional[OnboardingActor] = None,
) -> dict[str, int]:
    """Make every card of these students agree with the freeze mirror. Writes, never commits.

    Declarative on purpose: it does not ask "what changed", it asks "what should be true",
    which is what makes it safe to call on a redelivery, out of order, or twice. For each
    student:

    * every **open** card is paused if the mirror says its group is frozen, and resumed if it
      says otherwise — a student frozen on SAT keeps working their IELTS card, because the
      question is asked per group;
    * every **recently closed** card the freeze can explain is un-closed and then paused —
      see the race in this module's docstring.

    The caller commits. The pause and the mirror row it was derived from must land together,
    or a crash in between leaves the LMS showing a card for a student it knows is frozen.
    """
    ids = sorted({int(s) for s in student_ids})
    if not ids:
        return {"paused": 0, "resumed": 0, "reopened": 0}
    actor = actor or OnboardingActor.system("заморозка")
    index = freeze_view_for(db, ids)
    now = _utcnow()
    paused = resumed = reopened = 0

    open_rows = (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id.in_(ids),
            CuratorOnboarding.ended_at.is_(None),
        )
        .all()
    )
    for row in open_rows:
        should_pause = card_is_frozen(index, row)
        if should_pause and pause_cycle(db, row, actor):
            paused += 1
        elif not should_pause and resume_cycle(db, row, actor):
            resumed += 1

    # Only the recently closed can be candidates, and only when the mirror can explain them.
    # Bounded in SQL so a student with years of history is still one small query.
    closed_rows = (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id.in_(ids),
            CuratorOnboarding.ended_at.isnot(None),
            CuratorOnboarding.ended_at >= now - CLOSE_REVERSAL_WINDOW,
        )
        .order_by(CuratorOnboarding.ended_at.desc())
        .all()
    )
    for row in closed_rows:
        if not card_is_frozen(index, row):
            continue
        status = _reversible_close(db, row, now)
        if status is None:
            continue
        _reverse_close(db, row, status, actor)
        reopened += 1
        if pause_cycle(db, row, actor):
            paused += 1

    return {"paused": paused, "resumed": resumed, "reopened": reopened}


def describe(row: CuratorOnboarding, now: Optional[datetime] = None) -> dict[str, Any]:
    """The pause, for a log line or a diagnostic. Never a wire format on its own."""
    now = now or _utcnow()
    return {
        "onboarding_id": row.id,
        "is_paused": is_paused(row),
        "paused_at": row.paused_at.isoformat() if row.paused_at else None,
        "paused_seconds": int(paused_seconds_total(row, now)),
    }
