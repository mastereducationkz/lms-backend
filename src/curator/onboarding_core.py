"""Canonical curator-onboarding domain service.

Every writer goes through here — the legacy LMS kanban route, the CRM workspace (via
``/internal/crm/curator/*``) and the background reconciler — so there is exactly one place
that knows what an onboarding cycle is and when it may change. The CRM composes its own
financial data around what this module returns; it never re-implements the rules.

Two things this module owns that nothing else may duplicate:

**The cycle invariant.** At most one *open* row (``ended_at IS NULL``) per (curator,
student). Closed rows are history and are never revived — a student returning to a previous
curator gets a new row with the next ``cycle_no``. A **paused** card (a frozen student, see
:mod:`src.curator.onboarding_pause`) is still an open row and still holds that slot; it is
merely off the board with its clocks stopped.

**When a cycle ends.** Two ways, and they mean opposite things. The relationship ends and the
card is closed by the reconciler (``relationship_ended`` / ``transferred_out``), or the
curator finishes the job and marks «Завершено», which closes it as ``completed`` and keeps
the status ``done``. ``done`` is not a column to park in: leaving those rows open is how 312
finished cards came to be sitting on the board.

**The thresholds.** "Overdue" is a business rule, not a UI opinion, so the numbers live in
:data:`ONBOARDING_THRESHOLDS` and are served to the frontends over the API rather than being
re-typed into a component.

**What "finished" means here.** A group is finished for onboarding the moment its last
lesson ends — *not* when ``groups.is_over`` finally flips. The two differ by the completion
grace window (:mod:`src.services.group_completion_service`), which deliberately keeps a
finished group open to teachers, curators and admins until the following Wednesday. Reading
``is_over`` alone made that window mean "the relationship restarted": see
:func:`_finished_group_ids`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from src.schemas.models import (
    CuratorOnboarding,
    CuratorOnboardingEvent,
    CuratorOnboardingNote,
    Group,
    GroupStudent,
    UserInDB,
)

logger = logging.getLogger(__name__)

# --- statuses -----------------------------------------------------------------------------

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

#: Statuses that mean "someone still has to do something".
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS)
#: Columns of the board. ``cancelled`` is history and is hidden from the active board.
BOARD_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS, STATUS_DONE)
#: What a human may set a card to.
SETTABLE_STATUSES: tuple[str, ...] = (STATUS_NEW, STATUS_IN_PROGRESS, STATUS_DONE)

# --- end reasons --------------------------------------------------------------------------

END_RELATIONSHIP_ENDED = "relationship_ended"
END_TRANSFERRED_OUT = "transferred_out"
END_CURATOR_DEACTIVATED = "curator_deactivated"
END_LEGACY_CANCELLED = "legacy_cancelled"
END_MANUAL = "manual"
#: The curator finished the onboarding — «Завершено» closes the card. Its own reason because
#: it is the opposite of :data:`END_RELATIONSHIP_ENDED`: nothing was lost, the work was done.
#: A report counting how many students a curator stopped carrying must not count these, and
#: :func:`already_onboarded_into_group` reads it to know the pair is finished rather than
#: parted.
END_COMPLETED = "completed"
#: The cycle should never have been opened — see :mod:`src.curator.onboarding_repair`. Its own
#: reason rather than ``relationship_ended`` because the relationship did not end: it never
#: restarted, and a report counting how many students a curator lost must not include these.
END_OPENED_IN_ERROR = "opened_in_error"

# --- thresholds ---------------------------------------------------------------------------
#
# Centralised on purpose: these five numbers decide what the dashboards count, what the
# board badges, and what the overdue digest emails. Scattering them through UI components is
# how "overdue" starts meaning three different things on three screens.

#: A card sitting in ``new`` for longer than this many calendar days is overdue.
NEW_OVERDUE_DAYS = 2
#: A card in ``in_progress`` with no status movement for this many days is overdue.
IN_PROGRESS_STALE_DAYS = 5
#: No LMS activity for this many days raises the inactivity warning.
LMS_INACTIVITY_DAYS = 7
#: A product whose expected end falls within this many days is "ending soon".
PRODUCT_ENDING_SOON_DAYS = 15
#: A student studying at least this many distinct active products is flagged as multi-product.
MULTI_PRODUCT_MIN = 2

#: How far apart two timestamps must be before their order can be believed across tables.
#:
#: Not a business rule — a clock-skew tolerance, and the reason it has to exist:
#: ``group_students.created_at`` defaults to a timezone-**aware** ``datetime.now(timezone.utc)``
#: written into a **naive** ``DateTime`` column, so the driver renders it in the writing
#: session's timezone and Postgres stores local wall-clock. ``curator_onboarding.ended_at`` is
#: set explicitly from :func:`_utcnow`, which is naive UTC. The two columns are therefore in
#: different clocks — 8 hours apart on a UTC+8 developer machine, 0 on the UTC production
#: server — and a bare ``<=`` between them answers the wrong question wherever the app is not
#: running in UTC. 24h is the smallest round margin wider than the largest real offset (±14h).
#: Anything closer together than this is reported as "cannot tell", which
#: :func:`already_onboarded_into_group` treats as "do not veto".
_MEMBERSHIP_CLOCK_SKEW = timedelta(hours=24)

ONBOARDING_THRESHOLDS: dict[str, int] = {
    "new_overdue_days": NEW_OVERDUE_DAYS,
    "in_progress_stale_days": IN_PROGRESS_STALE_DAYS,
    "lms_inactivity_days": LMS_INACTIVITY_DAYS,
    "product_ending_soon_days": PRODUCT_ENDING_SOON_DAYS,
    "multi_product_min": MULTI_PRODUCT_MIN,
}


def _utcnow() -> datetime:
    """Naive UTC, matching the columns (which are all ``DateTime`` without timezone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Compare-safe: rows written before/after the tz-aware default are mixed in the wild."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# --- actor --------------------------------------------------------------------------------


@dataclass(frozen=True)
class OnboardingActor:
    """Who is making a change, in terms this module can record and authorise.

    Deliberately not a ``UserInDB``: the CRM calls in over the internal API with an identity
    it resolved itself, and the legacy LMS route calls in with a session user. Both collapse
    to (id, name, role), which is all the domain needs.
    """

    user_id: Optional[int]
    name: str = ""
    role: str = ""

    @property
    def is_head(self) -> bool:
        return self.role in ("head_curator", "admin")

    @classmethod
    def from_user(cls, user: UserInDB) -> "OnboardingActor":
        return cls(
            user_id=getattr(user, "id", None),
            name=(getattr(user, "official_full_name", None) or getattr(user, "name", "") or ""),
            role=(getattr(user, "role", "") or ""),
        )

    @classmethod
    def system(cls, name: str = "система") -> "OnboardingActor":
        return cls(user_id=None, name=name, role="system")


class OnboardingPermissionError(PermissionError):
    """The actor may not touch this card. Mapped to 403 by the route layer."""


class OnboardingNotFound(LookupError):
    """No such card, or none the actor is allowed to know exists. Mapped to 404."""


# --- history ------------------------------------------------------------------------------


def record_event(
    db: Session,
    onboarding: CuratorOnboarding,
    actor: OnboardingActor,
    action: str,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
) -> CuratorOnboardingEvent:
    """Append one history row. Caller commits — the event and the change it describes must
    land in the same transaction, or the log can disagree with the data."""
    event = CuratorOnboardingEvent(
        onboarding_id=onboarding.id,
        actor_id=actor.user_id,
        actor_name=actor.name or None,
        actor_role=actor.role or None,
        action=action,
        before=before,
        after=after,
        created_at=_utcnow(),
    )
    db.add(event)
    return event


# --- cycle management ---------------------------------------------------------------------


def already_onboarded_into_group(
    db: Session,
    curator_id: int,
    student_id: int,
    group_id: Optional[int],
) -> bool:
    """Has this curator already carried this student through onboarding *into this group*?

    The veto on re-opening a cycle, and deliberately the narrowest one that still stops the
    failure it exists for. It distinguishes the two ways a relationship can "reappear":

    *A student left and came back.* Their roster row was deleted and a new one written, so
    the membership is **newer** than the closed cycle. That is a genuine return and gets a
    genuine card.

    *Nothing about the student changed.* The roster row sat still and only the group's
    completion state wobbled — the grace window opened, a lesson was dragged into the future,
    a make-up lesson was appended. That is a blip, and a blip is not a new relationship.

    ``True`` therefore requires **all** of: the **most recent closed cycle** for the pair is
    on the **same group**, it reached :data:`STATUS_DONE`, and the student's current
    membership of that group **predates the cycle's close** by more than
    :data:`_MEMBERSHIP_CLOCK_SKEW` (the two timestamps come from different clocks — see that
    constant; a membership written close to the cutoff is reported as "cannot tell" and is
    not vetoed).

    One exception to the membership rule, for the cycle that closed *because it finished*
    (:data:`END_COMPLETED`). There the membership is expected to still be there — the student
    did not go anywhere — so "older than the close" says nothing, and the test becomes
    "newer than the close, by more than the skew" before a second cycle is allowed.

    Narrow on every axis, on purpose:

    * **Only the most recent cycle.** What matters is what happened last between these two
      people, not that they once shared a group in 2024.
    * **Only the same group.** Finishing a course and starting a *different* one with the
      same curator is a real new onboarding and still gets a card — the common case, and it
      must not be swallowed.
    * **Only ``done``.** A ``cancelled`` cycle means the onboarding never finished, so a
      second attempt is exactly right and is allowed through.
    * **Only when the roster says they never left.** This is what keeps a «перекурс» — a
      student genuinely re-enrolling in the same group under the same curator — working: the
      new membership row postdates the close, so no veto.
    * **No membership row at all → no veto.** Callers may open a cycle for a pair the roster
      does not describe (the CRM does, through ``/reconcile-student``). Absence is not
      evidence of a blip, and this must never veto on a guess.
    * **Nothing time-based.** A window would need a number nobody has agreed, and would go
      stale silently once chosen.

    Measured against production on 2026-09-08: of the 71 cycles the grace window re-opened,
    **all 71** had a membership older than the close — not one was a genuine return — and 61
    of them had a previous ``done`` cycle on the same group, so this would have refused those
    61, including **all 32** that were still open. The other 10 had a ``cancelled`` previous
    cycle and are stopped by :func:`_finished_group_ids` instead. Neither guard is redundant:
    this one also covers the older flap where a lesson dragged into the future re-opens a
    group that has genuinely finished, which the completion rule cannot see.
    """
    if group_id is None:
        # No group means no "same group" to compare against; never veto on a guess.
        return False
    previous = (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.curator_id == curator_id,
            CuratorOnboarding.student_id == student_id,
            CuratorOnboarding.ended_at.isnot(None),
        )
        .order_by(CuratorOnboarding.cycle_no.desc(), CuratorOnboarding.id.desc())
        .first()
    )
    if previous is None:
        return False
    if previous.group_id != group_id or previous.status != STATUS_DONE:
        return False

    joined = (
        db.query(GroupStudent.created_at)
        .filter(
            GroupStudent.group_id == group_id,
            GroupStudent.student_id == student_id,
        )
        .order_by(GroupStudent.created_at.desc().nullslast())
        .first()
    )
    if joined is None:
        return False
    joined_at = _as_naive_utc(joined[0])
    ended_at = _as_naive_utc(previous.ended_at)

    if previous.end_reason == END_COMPLETED:
        # The cycle closed because the curator finished it, not because the pair parted. The
        # student is *expected* to still be sitting in the group the onboarding settled them
        # into, so the age of their membership proves nothing on its own and the default has
        # to flip: veto unless the roster row is clearly **newer** than the close, which is
        # the only thing that can mean they left and came back.
        #
        # The ambiguous band leans the other way here for a reason. Failing to open a card
        # for a student who re-enrolled within hours of being marked «Завершено» costs one
        # card; opening one for every student a curator has just finished would put the whole
        # «Завершено» column straight back into «Новые», which is the failure this rule and
        # the repair command exist to end.
        if joined_at is None or ended_at is None:
            return True
        return joined_at <= ended_at + _MEMBERSHIP_CLOCK_SKEW

    if joined_at is None or ended_at is None:
        # A membership with no timestamp cannot be placed either side of the close. External
        # CRM inserts do produce these; treat the unknown as "may be a genuine return".
        return False
    return joined_at <= ended_at - _MEMBERSHIP_CLOCK_SKEW


def open_cycle(
    db: Session,
    curator_id: int,
    student_id: int,
    group_id: Optional[int],
    actor: Optional[OnboardingActor] = None,
) -> Optional[CuratorOnboarding]:
    """Start a fresh onboarding cycle, or return None if one is already open.

    Idempotent and concurrency-safe: the open-cycle uniqueness is enforced by a partial
    unique index, so two racing reconcilers cannot both create a card — the loser catches
    IntegrityError and finds the winner's row. Callers must be able to tolerate a
    ``SAVEPOINT`` here, which every caller in this repo can (they all run inside a session,
    not a raw connection).

    Returns ``None`` without writing when :func:`already_onboarded_into_group` vetoes the
    group — a relationship blip on a group this curator has already onboarded this student
    into is not a new relationship.
    """
    actor = actor or OnboardingActor.system()
    existing = active_cycle(db, curator_id, student_id)
    if existing is not None:
        return None
    if already_onboarded_into_group(db, curator_id, student_id, group_id):
        logger.info(
            "onboarding cycle refused: %s/%s were already onboarded into group %s",
            curator_id,
            student_id,
            group_id,
        )
        return None

    next_no = (
        db.query(func.coalesce(func.max(CuratorOnboarding.cycle_no), 0))
        .filter(
            CuratorOnboarding.curator_id == curator_id,
            CuratorOnboarding.student_id == student_id,
        )
        .scalar()
        or 0
    ) + 1

    now = _utcnow()
    row = CuratorOnboarding(
        curator_id=curator_id,
        student_id=student_id,
        group_id=group_id,
        status=STATUS_NEW,
        cycle_no=next_no,
        created_at=now,
        updated_at=now,
        status_changed_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # Another writer opened the cycle between our check and our insert. Theirs is as
        # good as ours; returning None means "nothing new here", which is the truth.
        logger.info(
            "onboarding cycle already opened concurrently curator=%s student=%s",
            curator_id,
            student_id,
        )
        return None

    record_event(
        db,
        row,
        actor,
        "cycle.opened",
        after={"cycle_no": next_no, "group_id": group_id, "status": STATUS_NEW},
    )
    return row


def close_cycle(
    db: Session,
    row: CuratorOnboarding,
    reason: str = END_RELATIONSHIP_ENDED,
    actor: Optional[OnboardingActor] = None,
) -> bool:
    """Close an open cycle. Returns False when it was already closed (idempotent).

    A cycle that reached ``done`` keeps that status — the onboarding genuinely finished and
    the history must say so. One still in flight becomes ``cancelled``, which is what the
    original reconciler did and what every existing report expects to see.

    A pause still running is settled into ``paused_seconds`` on the way out, so a closed
    card's totals are final and do not keep growing against a pause nobody will ever lift.
    """
    if row.ended_at is not None:
        return False
    actor = actor or OnboardingActor.system()
    before = {"status": row.status, "ended_at": None}
    now = _utcnow()
    if row.paused_at is not None:
        row.paused_seconds = int(paused_seconds_total(row, now))
        row.paused_at = None
    row.ended_at = now
    row.end_reason = reason
    if row.status in ACTIVE_STATUSES:
        row.status = STATUS_CANCELLED
        row.status_changed_at = now
    row.updated_at = now
    record_event(
        db,
        row,
        actor,
        "cycle.closed",
        before=before,
        after={"status": row.status, "ended_at": now.isoformat(), "end_reason": reason},
    )
    return True


def active_cycle(db: Session, curator_id: int, student_id: int) -> Optional[CuratorOnboarding]:
    """The one open cycle for a pair, if any."""
    return (
        db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.curator_id == curator_id,
            CuratorOnboarding.student_id == student_id,
            CuratorOnboarding.ended_at.is_(None),
        )
        .order_by(CuratorOnboarding.cycle_no.desc())
        .first()
    )


# --- reconciler ---------------------------------------------------------------------------


def _finished_group_ids(db: Session, group_ids: Iterable[int]) -> set[int]:
    """Of these groups, the ones that have taught out but are still inside the grace window.

    ``groups.is_over`` is not enough on its own. The completion grace period
    (:mod:`src.services.group_completion_service`) holds a finished group open — ``is_over``
    stays ``False`` — until the first Wednesday 23:59:59 Asia/Almaty after its last lesson
    ends, so that teachers do not lose the group off their list while attendance is still to
    be taken. For onboarding that window is not "still running": the course is over and the
    curator has nothing left to settle the student into.

    Reading ``is_over`` alone is what broke on 2026-09-08. Groups whose last lesson had ended
    days earlier had *already* had their cycles closed under the old rule; the grace window
    made them read as live again and the reconciler opened a second cycle for every student
    in them — 71 cards across 13 groups, every one a re-open of the same (curator, student,
    group) that had been closed days before.

    ``get_groups_close_deadlines`` returns a non-null deadline exactly for "finished, waiting
    for the cutoff", which is the set this returns. It is batched (two queries for any number
    of groups), so this stays off the N+1 path the reconciler used to be.
    """
    ids = sorted({int(g) for g in group_ids})
    if not ids:
        return set()
    # Imported here, not at module scope: the rule belongs to the completion service and this
    # module must never grow a second copy of it — the CRM already mirrors that one file
    # byte-for-byte and a third implementation would be the thing that drifts.
    from src.services.group_completion_service import get_groups_close_deadlines

    return {
        int(group_id)
        for group_id, deadline in get_groups_close_deadlines(db, ids).items()
        if deadline is not None
    }


def compute_active_pairs(db: Session) -> dict[tuple[int, int], int]:
    """``{(curator_id, student_id): group_id}`` for live curator↔student relationships.

    Live means: an active group that has a curator and has not finished teaching, containing
    an active student. Completion is honoured because a finished group is no longer anyone's
    responsibility — the original query checked only ``is_active``, so curators kept cards for
    cohorts that had finished months ago — and it is read through
    :func:`_finished_group_ids` rather than off ``is_over``, so a group sitting in its grace
    window counts as finished here even though the flag still says otherwise.

    When a student is in several groups owned by the same curator the most recently joined
    one wins as the card's *display* group; the relationship itself is the same either way.
    """
    rows = (
        db.query(
            Group.curator_id,
            GroupStudent.student_id,
            GroupStudent.group_id,
            GroupStudent.created_at,
        )
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .join(UserInDB, UserInDB.id == GroupStudent.student_id)
        .filter(
            Group.is_active == True,  # noqa: E712 - SQLAlchemy needs the comparison
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
            Group.curator_id.isnot(None),
            UserInDB.is_active == True,  # noqa: E712
        )
        .all()
    )
    # Dropped per row, before the "most recently joined wins" reduction below. Filtering the
    # reduced dict instead would lose the whole pair whenever a student's *display* group is
    # the finished one while another group under the same curator is still running.
    finished = _finished_group_ids(db, (group_id for _, _, group_id, _ in rows))

    pairs: dict[tuple[int, int], int] = {}
    seen_created: dict[tuple[int, int], datetime] = {}
    for curator_id, student_id, group_id, created_at in rows:
        if int(group_id) in finished:
            continue
        key = (int(curator_id), int(student_id))
        ts = _as_naive_utc(created_at) or datetime.min
        if key not in pairs or ts > seen_created[key]:
            pairs[key] = group_id
            seen_created[key] = ts
    return pairs


#: Arbitrary but fixed key for the Postgres advisory lock guarding the sweep.
_RECONCILE_LOCK_KEY = 0x0C0A_7E01


def _try_acquire_sweep_lock(db: Session) -> bool:
    """Claim the right to run the organisation-wide sweep, or report that someone else has it.

    The API runs four uvicorn workers and each starts its own reconciler thread, so the
    hourly sweep was running four times over. The *data* stayed correct — the partial unique
    index makes duplicate cycles impossible and ``close_cycle`` is idempotent — but each
    worker still appended its own ``cycle.closed`` event, so a card's history showed the
    same close four times, and the work was done four times for nothing.

    A session-level advisory lock is the cheap fix: it costs one round trip, needs no new
    table, and is released automatically if the worker dies. Non-PostgreSQL backends
    (SQLite, in tests) have no advisory locks and simply proceed — there is only ever one
    writer there.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return True
    try:
        return bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": _RECONCILE_LOCK_KEY}
            ).scalar()
        )
    except Exception:  # noqa: BLE001 - a lock we cannot take must not stop the sweep
        logger.warning("could not acquire reconcile advisory lock; proceeding", exc_info=True)
        return True


def _release_sweep_lock(db: Session) -> None:
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    try:
        db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _RECONCILE_LOCK_KEY})
    except Exception:  # noqa: BLE001
        logger.warning("could not release reconcile advisory lock", exc_info=True)


def reconcile_onboarding(db: Session, actor: Optional[OnboardingActor] = None) -> dict[str, int]:
    """Bring cards in line with live relationships. Idempotent; safe to run concurrently.

    * relationship appears and no cycle is open → open one (``new``)
    * relationship persists → refresh the display group only
    * relationship disappears → close the open cycle **unless the student is frozen on that
      group**, in which case the card pauses: a freeze deletes the membership, so absence is
      the expected state and closing it would throw away the curator's work
    * relationship reappears later → a *new* cycle, never a revived historical row

    Only one process performs the sweep at a time (see :func:`_try_acquire_sweep_lock`);
    the others return ``skipped`` rather than duplicating the work and its history.
    """
    actor = actor or OnboardingActor.system("реконсилятор")

    if not _try_acquire_sweep_lock(db):
        logger.info("onboarding reconcile skipped: another worker holds the sweep lock")
        return {
            "created": 0,
            "closed": 0,
            "regrouped": 0,
            "paused": 0,
            "resumed": 0,
            "skipped": 1,
        }

    try:
        return _reconcile_locked(db, actor)
    finally:
        _release_sweep_lock(db)


def _reconcile_locked(db: Session, actor: OnboardingActor) -> dict[str, int]:
    from src.curator.onboarding_pause import (
        card_is_frozen,
        freeze_view_for,
        pause_cycle,
        resume_cycle,
    )

    active = compute_active_pairs(db)

    open_rows: dict[tuple[int, int], CuratorOnboarding] = {}
    for row in db.query(CuratorOnboarding).filter(CuratorOnboarding.ended_at.is_(None)).all():
        open_rows[(int(row.curator_id), int(row.student_id))] = row

    # One query for every open card's student, so the freeze question below is a dict lookup
    # rather than a round trip per card.
    frozen = freeze_view_for(db, (r.student_id for r in open_rows.values()))
    created = closed = regrouped = paused = resumed = 0

    for key, group_id in active.items():
        row = open_rows.get(key)
        if row is None:
            if open_cycle(db, key[0], key[1], group_id, actor) is not None:
                created += 1
            continue
        if row.group_id != group_id:
            row.group_id = group_id  # keep the displayed group fresh
            regrouped += 1
        # Asked *after* the regroup: a student who came back into a different group of the
        # same curator is answered about the group they are actually in now, not the one
        # they were frozen out of. Both directions, so a sweep can repair a pause the
        # freeze-state delivery never arrived to apply — or to lift.
        if card_is_frozen(frozen, row):
            if pause_cycle(db, row, actor):
                paused += 1
        elif resume_cycle(db, row, actor):
            resumed += 1

    for key, row in open_rows.items():
        if key in active:
            continue
        if card_is_frozen(frozen, row):
            # The freeze removed the membership. Absence is what a freeze looks like from
            # here, and it is not the end of the relationship.
            if pause_cycle(db, row, actor):
                paused += 1
        elif close_cycle(db, row, END_RELATIONSHIP_ENDED, actor):
            closed += 1

    db.commit()
    return {
        "created": created,
        "closed": closed,
        "regrouped": regrouped,
        "paused": paused,
        "resumed": resumed,
        "skipped": 0,
    }


def reconcile_student(
    db: Session,
    student_id: int,
    actor: Optional[OnboardingActor] = None,
    commit: bool = True,
) -> dict[str, int]:
    """Reconcile one student's cards only — the cheap path after a group add/transfer.

    Same rules as the full sweep, restricted to a single student so a transfer does not have
    to wait for (or pay for) the hourly organisation-wide pass — **including** the freeze
    rule: the CRM calls this immediately after a freeze has removed the membership, so this
    is the code path that would otherwise close a frozen student's card within milliseconds
    of it being frozen.
    """
    from src.curator.onboarding_pause import (
        card_is_frozen,
        freeze_view_for,
        pause_cycle,
        resume_cycle,
    )

    actor = actor or OnboardingActor.system()
    all_pairs = compute_active_pairs(db)
    active = {k: v for k, v in all_pairs.items() if k[1] == int(student_id)}

    open_rows = {
        (int(r.curator_id), int(r.student_id)): r
        for r in db.query(CuratorOnboarding)
        .filter(
            CuratorOnboarding.student_id == int(student_id),
            CuratorOnboarding.ended_at.is_(None),
        )
        .all()
    }
    frozen = freeze_view_for(db, [int(student_id)])

    created = closed = paused = resumed = 0
    for key, group_id in active.items():
        row = open_rows.get(key)
        if row is None:
            if open_cycle(db, key[0], key[1], group_id, actor) is not None:
                created += 1
            continue
        if row.group_id != group_id:
            row.group_id = group_id
        if card_is_frozen(frozen, row):
            if pause_cycle(db, row, actor):
                paused += 1
        elif resume_cycle(db, row, actor):
            resumed += 1

    for key, row in open_rows.items():
        if key in active:
            continue
        if card_is_frozen(frozen, row):
            if pause_cycle(db, row, actor):
                paused += 1
        elif close_cycle(db, row, END_TRANSFERRED_OUT, actor):
            closed += 1

    if commit:
        db.commit()
    return {"created": created, "closed": closed, "paused": paused, "resumed": resumed}


# --- overdue ------------------------------------------------------------------------------


def is_paused(row: CuratorOnboarding) -> bool:
    """Is this card's clock stopped? See :mod:`src.curator.onboarding_pause`."""
    return getattr(row, "paused_at", None) is not None


def paused_seconds_total(row: CuratorOnboarding, now: Optional[datetime] = None) -> float:
    """Everything this cycle has spent paused, including a pause still running.

    The one place the two halves of the pause clock are added together — the settled
    ``paused_seconds`` and the open-ended stretch since ``paused_at``. While a card is paused
    this grows at exactly the rate wall-clock time does, which is what makes every
    ``elapsed - paused`` expression below stand still instead of ticking.
    """
    now = now or _utcnow()
    total = float(getattr(row, "paused_seconds", 0) or 0)
    started = _as_naive_utc(getattr(row, "paused_at", None))
    if started is not None:
        total += max(0.0, (now - started).total_seconds())
    return total


def _working_elapsed(
    row: CuratorOnboarding, anchor: Optional[datetime], now: datetime
) -> Optional[timedelta]:
    """Time since ``anchor`` with the paused stretches taken out, never negative."""
    if anchor is None:
        return None
    elapsed = (now - anchor) - timedelta(seconds=paused_seconds_total(row, now))
    return max(elapsed, timedelta(0))


def is_overdue(row: CuratorOnboarding, now: Optional[datetime] = None) -> bool:
    """Has this card breached its threshold? ``done``/closed/paused cards never are.

    Paused cards are excluded twice over, and both are deliberate: the explicit check below
    says a frozen student is nobody's overdue work *today*, and the pause arithmetic in
    :func:`_working_elapsed` makes sure the days they were away are not counted against the
    curator once the card comes back either.
    """
    if row.ended_at is not None or row.status not in ACTIVE_STATUSES:
        return False
    if is_paused(row):
        return False
    now = now or _utcnow()
    if row.status == STATUS_NEW:
        anchor = _as_naive_utc(row.created_at)
        limit = NEW_OVERDUE_DAYS
    else:
        anchor = _as_naive_utc(row.status_changed_at) or _as_naive_utc(row.updated_at)
        limit = IN_PROGRESS_STALE_DAYS
    elapsed = _working_elapsed(row, anchor, now)
    if elapsed is None:
        return False
    return elapsed > timedelta(days=limit)


def onboarding_age_days(row: CuratorOnboarding, now: Optional[datetime] = None) -> int:
    """How long this card has been somebody's work — frozen days excluded.

    The same reading as :func:`is_overdue`, because a card displaying «40 дней» while the
    board refuses to call it overdue is a contradiction a curator has to resolve by guessing.
    """
    now = now or _utcnow()
    elapsed = _working_elapsed(row, _as_naive_utc(row.created_at), now)
    if elapsed is None:
        return 0
    return max(0, elapsed.days)


# --- mutations ----------------------------------------------------------------------------


def _assert_may_edit(row: CuratorOnboarding, actor: OnboardingActor) -> None:
    """A card is editable by the curator who owns it, or by any head/admin.

    Head-curator edits are *interventions*: they are attributed to the head in the history
    but leave ``curator_id`` alone, because oversight is not a transfer of ownership.
    """
    if actor.is_head:
        return
    if actor.user_id is not None and int(row.curator_id) == int(actor.user_id):
        return
    raise OnboardingPermissionError("Карточка принадлежит другому куратору")


def get_card(db: Session, onboarding_id: int, actor: OnboardingActor) -> CuratorOnboarding:
    row = (
        db.query(CuratorOnboarding)
        .filter(CuratorOnboarding.id == onboarding_id)
        .first()
    )
    if row is None:
        raise OnboardingNotFound("Карточка не найдена")
    try:
        _assert_may_edit(row, actor)
    except OnboardingPermissionError:
        # Do not leak that a card with this id exists but belongs to someone else.
        raise OnboardingNotFound("Карточка не найдена") from None
    return row


def set_status(
    db: Session,
    row: CuratorOnboarding,
    status: str,
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboarding:
    """Move a card, and close the cycle when it reaches «Завершено».

    ``done`` is terminal, not a column to park in. It used to leave the row open until the
    relationship itself ended, which is how 312 finished cards came to be sitting on the
    board months after the work was done — an in-tray nobody could ever empty. Reaching
    ``done`` now ends the cycle through the one door, :func:`close_cycle`, which keeps the
    status ``done`` (it never rewrites a finished cycle to ``cancelled``) and records the
    close in the history like every other.

    Two consequences worth knowing before calling this:

    * the card leaves the board immediately — it is closed, and the board shows open cycles;
    * it cannot be dragged back afterwards. :func:`get_card` still finds it by id, but this
      function refuses to move a closed cycle, so «Завершено» is a decision rather than a
      column. A pair that genuinely starts again gets a *new* cycle, which is the invariant
      this module has always had.
    """
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"Недопустимый статус: {status}")
    _assert_may_edit(row, actor)
    if row.ended_at is not None:
        raise OnboardingPermissionError("Цикл закрыт — изменение статуса невозможно")
    if is_paused(row):
        # The student is frozen: the card is off the board and its clocks are stopped, so
        # there is nothing to report progress on. Refusing keeps the pause honest — a status
        # moved mid-pause would re-anchor the overdue clock inside a stretch of time the
        # curator was told not to work.
        raise OnboardingPermissionError(
            "Карточка на паузе: студент в заморозке — изменение статуса невозможно"
        )

    before = {"status": row.status}
    now = _utcnow()
    row.status = status
    row.status_changed_at = now
    row.updated_at = now
    if status == STATUS_DONE:
        row.completed_at = now
        row.completed_by = actor.user_id
    else:
        row.completed_at = None
        row.completed_by = None

    intervention = (
        actor.is_head and actor.user_id is not None and int(actor.user_id) != int(row.curator_id)
    )
    record_event(
        db,
        row,
        actor,
        "intervention" if intervention else "status.changed",
        before=before,
        after={"status": status},
    )
    if status == STATUS_DONE:
        close_cycle(db, row, END_COMPLETED, actor)
    if commit:
        db.commit()
        db.refresh(row)
    return row


def set_next_action(
    db: Session,
    row: CuratorOnboarding,
    next_action_at: Optional[date],
    note: Optional[str],
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboarding:
    _assert_may_edit(row, actor)
    before = {
        "next_action_at": row.next_action_at.isoformat() if row.next_action_at else None,
        "next_action_note": row.next_action_note,
    }
    row.next_action_at = next_action_at
    row.next_action_note = (note or "").strip()[:500] or None
    row.updated_at = _utcnow()
    record_event(
        db,
        row,
        actor,
        "next_action.set",
        before=before,
        after={
            "next_action_at": next_action_at.isoformat() if next_action_at else None,
            "next_action_note": row.next_action_note,
        },
    )
    if commit:
        db.commit()
        db.refresh(row)
    return row


def add_note(
    db: Session,
    row: CuratorOnboarding,
    body: str,
    actor: OnboardingActor,
    commit: bool = True,
) -> CuratorOnboardingNote:
    _assert_may_edit(row, actor)
    text = (body or "").strip()
    if not text:
        raise ValueError("Заметка не может быть пустой")
    note = CuratorOnboardingNote(
        onboarding_id=row.id,
        author_id=actor.user_id,
        author_name=actor.name or None,
        author_role=actor.role or None,
        body=text,
        created_at=_utcnow(),
    )
    db.add(note)
    row.updated_at = _utcnow()
    record_event(db, row, actor, "note.added", after={"length": len(text)})
    if commit:
        db.commit()
        db.refresh(note)
    return note


# --- reads --------------------------------------------------------------------------------


def board_query(
    db: Session,
    curator_ids: Optional[Sequence[int]] = None,
    statuses: Optional[Sequence[str]] = None,
    include_closed: bool = False,
    include_baseline: bool = False,
    include_paused: bool = False,
):
    """Base query for the board, with the launch-baseline and pause rules applied.

    The launch backfill seeded every pre-existing pair as ``done`` with no human actioner so
    the board would start clean. Those synthetic rows must stay hidden (they are not
    achievements anyone made) while genuinely completed cards — which always have
    ``completed_by`` — still show in Завершено.

    Paused cards are hidden too, and for the same kind of reason: a frozen student is not
    work anybody can do this week. They are *open* cycles, so they still hold the pair's one
    slot and nothing opens a second card alongside them — they are simply not on the board.
    ``include_paused`` exists for the reads that must see the whole open set anyway (repairs,
    diagnostics), never for a curator's screen.
    """
    q = db.query(CuratorOnboarding)
    if not include_closed:
        q = q.filter(CuratorOnboarding.ended_at.is_(None))
    if not include_paused:
        q = q.filter(CuratorOnboarding.paused_at.is_(None))
    q = q.filter(CuratorOnboarding.status.in_(list(statuses or BOARD_STATUSES)))
    if not include_baseline:
        q = q.filter(
            ~(
                (CuratorOnboarding.status == STATUS_DONE)
                & (CuratorOnboarding.completed_by.is_(None))
            )
        )
    if curator_ids is not None:
        ids = [int(c) for c in curator_ids]
        if not ids:
            # An empty explicit scope means "nothing", not "everything".
            return q.filter(CuratorOnboarding.id == -1)
        q = q.filter(CuratorOnboarding.curator_id.in_(ids))
    return q


def load_board(
    db: Session,
    curator_ids: Optional[Sequence[int]] = None,
    statuses: Optional[Sequence[str]] = None,
    student_ids: Optional[Sequence[int]] = None,
    include_closed: bool = False,
    include_paused: bool = False,
) -> list[CuratorOnboarding]:
    q = board_query(
        db,
        curator_ids=curator_ids,
        statuses=statuses,
        include_closed=include_closed,
        include_paused=include_paused,
    )
    if student_ids is not None:
        ids = [int(s) for s in student_ids]
        if not ids:
            return []
        q = q.filter(CuratorOnboarding.student_id.in_(ids))
    return (
        q.options(
            joinedload(CuratorOnboarding.student),
            joinedload(CuratorOnboarding.group),
            joinedload(CuratorOnboarding.curator),
        )
        .order_by(CuratorOnboarding.created_at.desc(), CuratorOnboarding.id.desc())
        .all()
    )


def status_counts(
    db: Session, curator_ids: Optional[Sequence[int]] = None
) -> dict[str, int]:
    """Per-status card counts for the dashboards, plus the derived ``overdue``."""
    rows = board_query(db, curator_ids=curator_ids).all()
    now = _utcnow()
    out = {s: 0 for s in BOARD_STATUSES}
    out["overdue"] = 0
    for row in rows:
        if row.status in out:
            out[row.status] += 1
        if is_overdue(row, now):
            out["overdue"] += 1
    return out


def curator_student_ids(db: Session, curator_ids: Sequence[int]) -> set[int]:
    """Students visible to these curators: everyone in their active, unfinished groups.

    This is the authorization primitive for the whole curator workspace — the CRM asks this
    question before it will show a student card, and the answer must not depend on whether an
    onboarding row happens to exist.

    "Unfinished" is :func:`_finished_group_ids`, the same reading :func:`compute_active_pairs`
    uses, so the two cannot disagree about whether a relationship exists. They are documented
    as answering one question and a card for a student the workspace will not show — or a
    student shown with no card the reconciler believes in — is the shape of that disagreement.
    """
    ids = [int(c) for c in curator_ids]
    if not ids:
        return set()
    rows = (
        db.query(GroupStudent.student_id, GroupStudent.group_id)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            Group.curator_id.in_(ids),
            Group.is_active == True,  # noqa: E712
            or_(Group.is_over == False, Group.is_over.is_(None)),  # noqa: E712
        )
        .distinct()
        .all()
    )
    finished = _finished_group_ids(db, (group_id for _, group_id in rows))
    return {int(student_id) for student_id, group_id in rows if int(group_id) not in finished}


def serialize_card(
    row: CuratorOnboarding,
    now: Optional[datetime] = None,
    include_notes: bool = False,
    include_history: bool = False,
) -> dict[str, Any]:
    """Wire shape shared by the LMS kanban and the CRM workspace."""
    now = now or _utcnow()
    student = row.student
    data: dict[str, Any] = {
        "id": row.id,
        "cycle_no": row.cycle_no or 1,
        "student_id": row.student_id,
        "student_name": (
            (student.official_full_name or student.name) if student else ""
        ),
        "student_email": (getattr(student, "email", None) if student else None),
        "group_id": row.group_id,
        "group_name": row.group.name if row.group else None,
        "curator_id": row.curator_id,
        "curator_name": (row.curator.name if row.curator else None),
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "status_changed_at": (
            row.status_changed_at.isoformat() if row.status_changed_at else None
        ),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "completed_by": row.completed_by,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "end_reason": row.end_reason,
        # The pause is on the wire so a card fetched by id can say why it is not on the
        # board — a detail view that showed nothing would look like a bug to the curator
        # who bookmarked it.
        "is_paused": is_paused(row),
        "paused_at": row.paused_at.isoformat() if row.paused_at else None,
        "paused_days": int(paused_seconds_total(row, now) // 86400),
        "next_action_at": row.next_action_at.isoformat() if row.next_action_at else None,
        "next_action_note": row.next_action_note,
        "age_days": onboarding_age_days(row, now),
        "is_overdue": is_overdue(row, now),
        "last_activity_date": (
            student.last_activity_date.isoformat()
            if student is not None and getattr(student, "last_activity_date", None)
            else None
        ),
    }
    if include_notes:
        data["notes"] = [
            {
                "id": n.id,
                "body": n.body,
                "author_id": n.author_id,
                "author_name": n.author_name,
                "author_role": n.author_role,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in (row.notes or [])
        ]
    if include_history:
        data["history"] = [
            {
                "id": e.id,
                "action": e.action,
                "actor_id": e.actor_id,
                "actor_name": e.actor_name,
                "actor_role": e.actor_role,
                "before": e.before,
                "after": e.after,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in (row.events or [])
        ]
    return data


def telegram_link(tg: Optional[str]) -> Optional[str]:
    """A t.me URL for a handle, or None for a numeric chat id / empty value."""
    if not tg:
        return None
    handle = tg.strip().lstrip("@")
    if not handle or handle.isdigit():
        return None
    return f"https://t.me/{handle}"
