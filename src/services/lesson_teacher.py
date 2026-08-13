"""The occurrence-level lesson-teacher invariant.

Two different facts share the name "teacher" and were being conflated:

* ``groups.teacher_id`` — the *regular* teacher, the person responsible for running the
  group. Ownership. It decides the pay band and who the group belongs to on a report.
* ``events.teacher_id`` — the person who actually stands in front of *this* lesson. It
  decides who owes attendance for it and who gets paid for it.

An approved substitution changes the second and must never change the first. The bug this
module exists to prevent is the reverse: schedule regeneration re-derived
``events.teacher_id`` from ``groups.teacher_id`` and silently undid every approved
substitution for a future lesson. Concretely, group "June 15 SAT - Azamat" had Lesson 32
covered by Nuray; the next schedule edit handed it back to Azamat, who then saw it in his
unmarked-attendance queue and (once marked) in his salary, while Nuray saw neither.

So: **the approved substitution is the authority for that occurrence, and every writer of
``events.teacher_id`` must ask this module first.** The rule lives here rather than as an
``if`` at each call site because there are six such call sites across two repositories, and
the last incident was caused by fixing four of them.

A later *deliberate* teacher change is still possible — see :func:`override_lesson_teacher` —
but it has to be explicit and audited. What must not happen is a bulk regeneration quietly
reverting a decision a head teacher made.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import exists as sa_exists
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Request types that pin a teacher to a single occurrence.
SUBSTITUTION_REQUEST_TYPE = "substitution"

#: Only an *approved* request is authoritative. Pending requests are proposals and rejected
#: ones are decisions to leave the lesson alone; neither may move a teacher.
APPROVED_STATUS = "approved"


def approved_substitution_exists_clause():
    """A correlated ``EXISTS`` that is true for events carrying an approved substitution.

    Use inside a query already selecting from ``Event`` — e.g.
    ``query.filter(~approved_substitution_exists_clause())`` to exclude protected events from
    a bulk teacher rewrite. Correlated rather than a subquery of ids so it stays cheap on the
    UPDATE ... WHERE path.
    """
    from src.events.models import Event
    from src.lesson_requests.models import LessonRequest

    return sa_exists().where(
        (LessonRequest.event_id == Event.id)
        & (LessonRequest.request_type == SUBSTITUTION_REQUEST_TYPE)
        & (LessonRequest.status == APPROVED_STATUS)
    )


def approved_substitute_id(lr) -> Optional[int]:
    """The teacher an approved request actually appoints.

    ``confirmed_teacher_id`` wins when present: the flow allows a head teacher to pick one
    name out of ``substitute_teacher_ids`` at approval time, and that choice supersedes the
    single ``substitute_teacher_id`` the requester proposed.
    """
    return lr.confirmed_teacher_id or lr.substitute_teacher_id


def approved_substitution_map(
    db: Session, event_ids: Optional[Iterable[int]] = None
) -> dict[int, int]:
    """``{event_id: substitute_teacher_id}`` for events with exactly one approved substitution.

    Events carrying *conflicting* approved requests (two approvals naming different teachers)
    are deliberately omitted: there is no safe answer, and guessing one would silently pick a
    winner for a payroll decision. :func:`conflicting_substitution_event_ids` reports them so
    a human can resolve them, and callers that preserve overrides fall back to leaving such an
    event exactly as it is.
    """
    from src.lesson_requests.models import LessonRequest

    query = db.query(
        LessonRequest.event_id,
        LessonRequest.confirmed_teacher_id,
        LessonRequest.substitute_teacher_id,
    ).filter(
        LessonRequest.event_id.isnot(None),
        LessonRequest.request_type == SUBSTITUTION_REQUEST_TYPE,
        LessonRequest.status == APPROVED_STATUS,
    )
    ids = _as_id_list(event_ids)
    if ids is not None:
        if not ids:
            return {}
        query = query.filter(LessonRequest.event_id.in_(ids))

    chosen: dict[int, int] = {}
    conflicted: set[int] = set()
    for event_id, confirmed_id, proposed_id in query.all():
        teacher_id = confirmed_id or proposed_id
        if teacher_id is None:
            # An approved substitution that never named anybody cannot pin a teacher. It is
            # reported as a defect by the repair command rather than treated as authority.
            continue
        event_id = int(event_id)
        teacher_id = int(teacher_id)
        previous = chosen.get(event_id)
        if previous is not None and previous != teacher_id:
            conflicted.add(event_id)
            continue
        chosen[event_id] = teacher_id

    for event_id in conflicted:
        chosen.pop(event_id, None)
    return chosen


def conflicting_substitution_event_ids(
    db: Session, event_ids: Optional[Iterable[int]] = None
) -> dict[int, list[int]]:
    """``{event_id: [teacher_id, ...]}`` where approved requests disagree about the teacher.

    Never repaired automatically — reported for a human. Two people approved two different
    substitutes for one lesson and only they know which one taught it.
    """
    from src.lesson_requests.models import LessonRequest

    query = db.query(
        LessonRequest.event_id,
        LessonRequest.confirmed_teacher_id,
        LessonRequest.substitute_teacher_id,
    ).filter(
        LessonRequest.event_id.isnot(None),
        LessonRequest.request_type == SUBSTITUTION_REQUEST_TYPE,
        LessonRequest.status == APPROVED_STATUS,
    )
    ids = _as_id_list(event_ids)
    if ids is not None:
        if not ids:
            return {}
        query = query.filter(LessonRequest.event_id.in_(ids))

    seen: dict[int, set[int]] = {}
    for event_id, confirmed_id, proposed_id in query.all():
        teacher_id = confirmed_id or proposed_id
        if teacher_id is None:
            continue
        seen.setdefault(int(event_id), set()).add(int(teacher_id))
    return {
        event_id: sorted(teacher_ids)
        for event_id, teacher_ids in seen.items()
        if len(teacher_ids) > 1
    }


def protected_event_teachers(db: Session, group_id: int) -> dict[int, int]:
    """``{event_id: teacher_id}`` of occurrence overrides inside one group.

    What a schedule regeneration must leave alone. Includes conflicted events mapped to their
    *current* ``events.teacher_id`` so that "leave it exactly as it is" is expressible with
    the same dict the unambiguous cases use — a reconciliation must not resolve a conflict by
    accident either.
    """
    from src.events.models import Event, EventGroup

    event_ids = [
        int(row[0])
        for row in db.query(EventGroup.event_id).filter(EventGroup.group_id == group_id).all()
    ]
    if not event_ids:
        return {}

    protected = approved_substitution_map(db, event_ids)
    conflicts = conflicting_substitution_event_ids(db, event_ids)
    if conflicts:
        current = dict(
            db.query(Event.id, Event.teacher_id)
            .filter(Event.id.in_(list(conflicts.keys())))
            .all()
        )
        for event_id in conflicts:
            existing = current.get(event_id)
            if existing is not None:
                protected[int(event_id)] = int(existing)
    return protected


def assign_lesson_teacher_preserving_overrides(
    event, group_teacher_id: Optional[int], protected: dict[int, int]
) -> None:
    """Point one event at the group teacher **unless** the occurrence is overridden.

    The single line every reconciliation loop should call instead of
    ``event.teacher_id = teacher_id``. Idempotent: an event already carrying its approved
    substitute is left untouched, so re-running reconciliation changes nothing.
    """
    override = protected.get(int(event.id)) if event.id is not None else None
    if override is not None:
        if event.teacher_id != override:
            logger.info(
                "reconciliation restored occurrence override event_id=%s teacher_id=%s "
                "(was %s)",
                event.id,
                override,
                event.teacher_id,
            )
            event.teacher_id = override
        return
    event.teacher_id = group_teacher_id


def record_lesson_teacher_audit(
    db: Session,
    *,
    event_id: int,
    before_teacher_id: Optional[int],
    after_teacher_id: Optional[int],
    action: str,
    actor_id: Optional[int],
    requester_id: Optional[int] = None,
    lesson_request_id: Optional[int] = None,
    reason: Optional[str] = None,
) -> None:
    """Write the domain-level audit row for a lesson-teacher change.

    The database trigger on ``events`` already records *that* ``teacher_id`` changed, with
    before and after. What it cannot know is *why*, *who asked* and *who approved* — the
    trigger sees an UPDATE, not a decision. This row carries that, and shares the outbox so
    the CRM receives both through one delivery path.

    Never raises into the caller: an audit problem must not turn an approved substitution
    into a failed request. The write is inside the caller's transaction, so it commits with
    the assignment or not at all.
    """
    try:
        from src.crm_audit.models import CrmAuditOutbox

        db.add(
            CrmAuditOutbox(
                event_id=f"lms-subst-{uuid.uuid4().hex}",
                action=action,
                payload={
                    "action": action,
                    "entity_type": "lesson",
                    "entity_id": int(event_id),
                    "actor_id": actor_id,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "before": {"teacher_id": before_teacher_id},
                    "after": {"teacher_id": after_teacher_id},
                    "requester_id": requester_id,
                    "lesson_request_id": lesson_request_id,
                    "reason": reason,
                },
            )
        )
    except Exception:  # pragma: no cover - defensive, audit must never break the mutation
        logger.exception("lesson-teacher audit row failed for event %s", event_id)


def override_lesson_teacher(
    db: Session,
    *,
    event,
    new_teacher_id: Optional[int],
    actor_id: Optional[int],
    reason: str,
) -> None:
    """Deliberately re-point one lesson at a different teacher, superseding a substitution.

    The escape hatch for "the substitute also fell ill". It is a distinct, audited action
    precisely so that it cannot happen by accident during schedule regeneration: bulk paths
    call :func:`assign_lesson_teacher_preserving_overrides`, which refuses; a human doing this
    on purpose calls here and leaves a record naming themselves.
    """
    before = event.teacher_id
    if before == new_teacher_id:
        return
    event.teacher_id = new_teacher_id
    record_lesson_teacher_audit(
        db,
        event_id=event.id,
        before_teacher_id=before,
        after_teacher_id=new_teacher_id,
        action="lesson.teacher.overridden",
        actor_id=actor_id,
        reason=reason,
    )


def _as_id_list(event_ids: Optional[Iterable[int]]) -> Optional[list[int]]:
    if event_ids is None:
        return None
    return sorted({int(event_id) for event_id in event_ids if event_id is not None})
