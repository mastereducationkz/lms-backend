"""Close the onboarding cycles the completion grace window opened by mistake.

On 2026-09-08 the group-completion grace shipped (lms-backend #83, crm-master #88). It holds
a finished group open — ``groups.is_over`` stays ``False`` — until the first Wednesday
23:59:59 Asia/Almaty after its last lesson ends, so teachers do not lose the group off their
list while attendance is still to be taken.

Thirteen groups had already finished teaching *before* that shipped, between the previous
Wednesday cutoff and the deploy. Under the old rule they were ``is_over = True`` and the
reconciler had closed their students' onboarding cycles days earlier — 18 on Sep 4, 31 on
Sep 5, 22 on Sep 7. The grace window made them read as live again, and the 08:36 and 09:36
sweeps opened **71 fresh cycles**, every one a re-open of the same (curator, student, group)
that had just been closed. The board's «Новые» went from 11 to 78 in three hundred
milliseconds.

:func:`~src.curator.onboarding_core._finished_group_ids` stops it happening again. The cards
already written are still on curators' boards, and this closes them.

**Only the cards that are demonstrably the deploy's fault, and only the ones nobody has
touched.** Every condition is a reason to leave a row alone if it does not hold:

``close``
    Open, still ``new``, ``cycle_no > 1``, created inside the deploy window, on a group that
    is *currently* inside its completion grace window, with no sign a human has worked it.
    That conjunction describes the failure and nothing else.

``skipped: curator started work``
    ``status`` has moved off ``new``. A curator has picked the card up — possibly all the way
    to «Завершено», which in the CRM also writes the student's study status. Closing it now
    would erase work somebody did, and the card is theirs to finish or cancel.

``skipped: human activity``
    A note exists, or the history has an event with a real ``actor_id`` after the card was
    created. The status may not have moved but somebody has been here; that is a person's
    business, not a repair's.

``skipped: first cycle``
    ``cycle_no == 1``. Nothing was re-opened — this is a student's first onboarding with this
    curator and very likely a genuine September enrolment. Six such cards were created on
    2026-09-08 and none of them is a mistake.

``skipped: group is running``
    The group is not inside a grace window today: it has lessons still to teach, or it has
    already closed. Either way the card is not evidence of this bug.

``skipped: outside the window``
    Created before the deploy or after ``--until``. Not this incident.

``skipped: already closed``
    ``ended_at`` is set. This is what makes a second run a no-op, and it is also the honest
    answer for the 39 cards the ordinary reconciler already closed on its own once their
    groups were deactivated by hand.

**Audited through the domain, not around it.** Closing goes through
:func:`~src.curator.onboarding_core.close_cycle`, so each card gets the same
``cycle.closed`` history row every other close writes, attributed to this command. The
``end_reason`` is :data:`~src.curator.onboarding_core.END_OPENED_IN_ERROR`, which exists so
these are distinguishable forever from a relationship that genuinely ended.

**Idempotent, and re-checked at the moment of writing.** The scan may be minutes old by the
time somebody reads the table and types ``--apply``; every condition is evaluated again per
row inside the apply loop, so nothing is closed on the strength of the report alone.

Baseline to compare a dry run against — **76 cards in the window, measured on production at
2026-09-08 13:23 UTC**:

===== ================================================= ==============================
count what                                              expected verdict
===== ================================================= ==============================
10    240 July 13 SAT - Mukhamedyarova, cycle 2, ``new`` close
1     289 Dastan SAT 2026 - Бексултан, cycle 2, ``new``  close
10    cycle 2 cards a curator moved to «Завершено»       skipped: curator started work
5     cycle 1 cards — September's real new students      skipped: first cycle
50    already closed by the ordinary reconciler          skipped: already closed
===== ================================================= ==============================

This number moves on its own, and downwards. The reconciler closes these cards itself as
soon as their group leaves the live set — because the grace deadline passes (all thirteen
groups close at Wed 2026-09-09 18:59:59 UTC) or because somebody deactivates the group by
hand, which is what happened to group 172 and its eleven cards between 12:15 and 13:23 UTC.
An hour earlier this table read 22 / 10 / 6 / 39. **A dry run reporting fewer closes than
this is the expected direction**; one reporting *more*, or naming groups other than the
thirteen, means something new has happened — read the difference before applying.

The nine groups inside a grace window at the time of measurement: 146, 150, 167, 172, 240,
248, 266, 289, 340.

Usage::

    python -m src.curator.onboarding_repair                      # dry run, prints a table
    python -m src.curator.onboarding_repair --json
    python -m src.curator.onboarding_repair --apply --actor-email admin@example.com
    python -m src.curator.onboarding_repair --since 2026-09-08T07:36:00 --until 2026-09-09
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from src.curator.onboarding_core import (
    END_OPENED_IN_ERROR,
    STATUS_NEW,
    OnboardingActor,
    _finished_group_ids,
    close_cycle,
)
from src.schemas.models import (
    CuratorOnboarding,
    CuratorOnboardingEvent,
    CuratorOnboardingNote,
    Group,
    UserInDB,
)

logger = logging.getLogger(__name__)

#: Bumped when the eligibility rules change, so a report says which rules produced it.
REPAIR_VERSION = "onboarding-grace-repair-1"

#: The history action :func:`~src.curator.onboarding_core.open_cycle` writes. Never counts as
#: somebody having worked the card — it *is* the card appearing.
EVENT_CYCLE_OPENED = "cycle.opened"

#: When the grace window went live: lms-backend #83 merged 07:33:02 UTC and the API container
#: came up at 07:36:01 UTC. The first cycle it wrongly opened was at 08:36. Overridable, but
#: this is the instant the incident starts.
DEPLOY_UTC = datetime(2026, 9, 8, 7, 36, 0)

VERDICT_CLOSE = "close"
VERDICT_ALREADY_CLOSED = "skipped: already closed"
VERDICT_CURATOR_WORKING = "skipped: curator started work"
VERDICT_HUMAN_ACTIVITY = "skipped: human activity"
VERDICT_FIRST_CYCLE = "skipped: first cycle"
VERDICT_GROUP_RUNNING = "skipped: group is running"
VERDICT_OUTSIDE_WINDOW = "skipped: outside the window"


@dataclass
class Finding:
    """One candidate card, the evidence about it, and the verdict."""

    onboarding_id: int
    curator_id: int
    student_id: int
    group_id: Optional[int]
    group_name: str
    cycle_no: int
    status: str
    created_at: Optional[str]
    ended_at: Optional[str]
    #: True when the group is finished-but-inside-its-grace-window right now.
    group_in_grace: bool
    notes: int
    #: History rows with a real ``actor_id``, written after the card was created.
    human_events: int
    verdict: str

    @property
    def is_actionable(self) -> bool:
        return self.verdict == VERDICT_CLOSE


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _human_event_counts(db: Session, onboarding_ids: Iterable[int]) -> dict[int, int]:
    """Per card, history rows a person wrote after it was created.

    ``actor_id IS NULL`` is the system: the reconciler opens every cycle as
    ``OnboardingActor.system()``. Anything with an id is a human, and a human who has been
    near this card is a reason not to touch it. Compared against each row's own
    ``created_at`` rather than a global instant, because a card opened at 09:36 and a card
    opened at 08:36 have different "after".

    ``cycle.opened`` is excluded whoever wrote it. It is the card's own creation, not work on
    the card, and it carries a real ``actor_id`` whenever the CRM opened the cycle (an
    ``assign-curator`` call attributes it to the head who made it) — counting that would make
    every CRM-created card permanently unrepairable for the wrong reason.
    """
    ids = [int(i) for i in onboarding_ids]
    if not ids:
        return {}
    counts: dict[int, int] = {}
    rows = (
        db.query(
            CuratorOnboardingEvent.onboarding_id,
            CuratorOnboardingEvent.created_at,
            CuratorOnboarding.created_at,
        )
        .join(
            CuratorOnboarding,
            CuratorOnboarding.id == CuratorOnboardingEvent.onboarding_id,
        )
        .filter(
            CuratorOnboardingEvent.onboarding_id.in_(ids),
            CuratorOnboardingEvent.actor_id.isnot(None),
            CuratorOnboardingEvent.action != EVENT_CYCLE_OPENED,
        )
        .all()
    )
    for onboarding_id, event_at, card_at in rows:
        if event_at is not None and card_at is not None and event_at <= card_at:
            continue
        counts[int(onboarding_id)] = counts.get(int(onboarding_id), 0) + 1
    return counts


def _note_counts(db: Session, onboarding_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(i) for i in onboarding_ids]
    if not ids:
        return {}
    counts: dict[int, int] = {}
    for (onboarding_id,) in (
        db.query(CuratorOnboardingNote.onboarding_id)
        .filter(CuratorOnboardingNote.onboarding_id.in_(ids))
        .all()
    ):
        counts[int(onboarding_id)] = counts.get(int(onboarding_id), 0) + 1
    return counts


def scan(
    db: Session,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    only_ids: Optional[Iterable[int]] = None,
) -> list[Finding]:
    """Every cycle created in the window, classified. Writes nothing.

    The window is the only thing narrowed in SQL. Everything else is evaluated in Python and
    reported, because a row that *looks* like a candidate and is skipped is the most useful
    line in the table — it is the one a human has to agree with.

    ``only_ids`` re-classifies specific cards with the identical rules and ignores the window
    bounds for *selection* (they still decide the ``outside the window`` verdict). The apply
    loop uses it so the check guarding the write cannot drift from the check that produced
    the report.
    """
    since = since or DEPLOY_UTC
    query = db.query(CuratorOnboarding)
    if only_ids is not None:
        ids = [int(i) for i in only_ids]
        if not ids:
            return []
        query = query.filter(CuratorOnboarding.id.in_(ids))
    else:
        # A generous SQL bound, then the exact verdict in Python: a row just outside the
        # window is worth printing as `outside the window` rather than silently vanishing.
        query = query.filter(
            CuratorOnboarding.created_at.isnot(None),
            CuratorOnboarding.created_at >= since,
        )
        if until is not None:
            query = query.filter(CuratorOnboarding.created_at <= until)
    rows = query.order_by(CuratorOnboarding.created_at.asc(), CuratorOnboarding.id.asc()).all()
    if not rows:
        return []

    group_ids = {int(r.group_id) for r in rows if r.group_id is not None}
    in_grace = _finished_group_ids(db, group_ids)
    group_names = {
        int(g.id): (g.name or "").strip()
        for g in db.query(Group).filter(Group.id.in_(group_ids)).all()
    }
    notes = _note_counts(db, [r.id for r in rows])
    human_events = _human_event_counts(db, [r.id for r in rows])

    findings: list[Finding] = []
    for row in rows:
        group_id = int(row.group_id) if row.group_id is not None else None
        grace = group_id is not None and group_id in in_grace
        note_count = notes.get(int(row.id), 0)
        event_count = human_events.get(int(row.id), 0)

        # Ordered most-specific first: the first true reason is the one printed, and
        # "somebody already dealt with this" must win over "it matched the pattern".
        if row.ended_at is not None:
            verdict = VERDICT_ALREADY_CLOSED
        elif row.created_at is None or row.created_at < since:
            verdict = VERDICT_OUTSIDE_WINDOW
        elif until is not None and row.created_at > until:
            verdict = VERDICT_OUTSIDE_WINDOW
        elif int(row.cycle_no or 1) <= 1:
            verdict = VERDICT_FIRST_CYCLE
        elif row.status != STATUS_NEW:
            verdict = VERDICT_CURATOR_WORKING
        elif note_count or event_count:
            verdict = VERDICT_HUMAN_ACTIVITY
        elif not grace:
            verdict = VERDICT_GROUP_RUNNING
        else:
            verdict = VERDICT_CLOSE

        findings.append(
            Finding(
                onboarding_id=int(row.id),
                curator_id=int(row.curator_id),
                student_id=int(row.student_id),
                group_id=group_id,
                group_name=(
                    group_names.get(group_id) or (f"#{group_id}" if group_id else "—")
                ),
                cycle_no=int(row.cycle_no or 1),
                status=row.status,
                created_at=_iso(row.created_at),
                ended_at=_iso(row.ended_at),
                group_in_grace=grace,
                notes=note_count,
                human_events=event_count,
                verdict=verdict,
            )
        )
    return findings


def apply_repair(
    db: Session,
    findings: list[Finding],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    actor: Optional[OnboardingActor] = None,
) -> list[Finding]:
    """Close the cards the scan cleared, one committed transaction each.

    Per-card rather than one transaction for all of them: these are unrelated students, and
    one row that fails must not roll back the others.
    """
    actor = actor or OnboardingActor.system("исправление онбординга")
    since = since or DEPLOY_UTC
    closed: list[Finding] = []

    for finding in findings:
        if not finding.is_actionable:
            continue

        # Re-read and re-decide. Between the scan and this line a curator may have opened the
        # card, written a note, or the reconciler may have closed it.
        fresh = scan_one(db, finding.onboarding_id, since=since, until=until)
        if fresh is None or not fresh.is_actionable:
            logger.info(
                "onboarding %s: no longer eligible (%s), left alone",
                finding.onboarding_id,
                fresh.verdict if fresh else "row is gone",
            )
            finding.verdict = fresh.verdict if fresh else "skipped: row is gone"
            continue

        row = (
            db.query(CuratorOnboarding)
            .filter(CuratorOnboarding.id == finding.onboarding_id)
            .first()
        )
        if row is None:
            continue
        try:
            if not close_cycle(db, row, END_OPENED_IN_ERROR, actor):
                db.rollback()
                continue
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one bad row must not abort the rest
            db.rollback()
            logger.warning(
                "onboarding %s: close failed, left untouched",
                finding.onboarding_id,
                exc_info=True,
            )
            print(f"  ! onboarding {finding.onboarding_id}: {exc}", file=sys.stderr)
            continue

        finding.ended_at = _iso(row.ended_at)
        finding.status = row.status
        closed.append(finding)
    return closed


def scan_one(
    db: Session,
    onboarding_id: int,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
) -> Optional[Finding]:
    """Re-classify a single card with the identical rules :func:`scan` uses."""
    found = scan(db, since=since, until=until, only_ids=[int(onboarding_id)])
    return found[0] if found else None


def _summary(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    return {
        "repair_version": REPAIR_VERSION,
        "generated_at": datetime.utcnow().isoformat(),
        "cards_in_window": len(findings),
        "would_close": counts.get(VERDICT_CLOSE, 0),
        "by_verdict": dict(sorted(counts.items())),
    }


def render_table(findings: list[Finding]) -> str:
    """The report a human reads before typing ``--apply``.

    Every column is the evidence for the verdict in the last one — the cycle number, the
    status, whether the group is in its grace window, and whether a person has been near the
    card. A verdict with nothing behind it is not reviewable.
    """
    headers = [
        "card", "curator", "student", "group", "cycle",
        "status", "created", "grace", "notes", "human", "verdict",
    ]
    rows = [
        [
            str(f.onboarding_id),
            str(f.curator_id),
            str(f.student_id),
            f"{f.group_id or '—'} {f.group_name}".strip(),
            str(f.cycle_no),
            f.status,
            (f.created_at or "")[:19],
            "yes" if f.group_in_grace else "no",
            str(f.notes),
            str(f.human_events),
            f.verdict,
        ]
        for f in findings
    ]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    lines = [
        "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
        "  ".join("-" * w for w in widths),
    ]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]

    summary = _summary(findings)
    lines += ["", f"cards in window: {summary['cards_in_window']}"]
    for verdict, count in summary["by_verdict"].items():
        lines.append(f"  {verdict}: {count}")
    if not summary["would_close"]:
        lines += ["", "nothing to close — every card in the window has a reason to stay."]
    return "\n".join(lines)


def _report_json(findings: list[Finding]) -> str:
    return json.dumps(
        {**_summary(findings), "findings": [asdict(f) for f in findings]},
        indent=2,
        ensure_ascii=False,
    )


def _parse_moment(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not an ISO-8601 instant: {raw!r} (e.g. 2026-09-08T07:36:00)"
        ) from None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Close the onboarding cycles the 2026-09-08 completion-grace deploy opened by "
            "mistake. Dry run by default."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="close the eligible cards (default is a dry run that writes nothing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report only; the default, accepted so it can be stated explicitly",
    )
    parser.add_argument("--json", action="store_true", help="JSON instead of the table")
    parser.add_argument(
        "--since",
        type=_parse_moment,
        default=None,
        help=f"start of the deploy window, naive UTC (default {DEPLOY_UTC.isoformat()})",
    )
    parser.add_argument(
        "--until", type=_parse_moment, default=None, help="end of the window, naive UTC"
    )
    parser.add_argument(
        "--actor-email",
        default=None,
        help="LMS user to attribute the closes to (default: recorded as the system)",
    )
    args = parser.parse_args(argv)

    if args.apply and args.dry_run:
        print("--apply and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from src.config import SessionLocal

    db = SessionLocal()
    try:
        since = args.since or DEPLOY_UTC
        findings = scan(db, since=since, until=args.until)
        print(_report_json(findings) if args.json else render_table(findings))

        if not args.apply:
            if not args.json:
                print("\nDRY RUN — nothing was written. Re-run with --apply to close them.")
            return 0

        actor = None
        if args.actor_email:
            user = (
                db.query(UserInDB).filter(UserInDB.email == args.actor_email).first()
            )
            if user is None:
                print(f"no LMS user with email {args.actor_email}", file=sys.stderr)
                return 2
            actor = OnboardingActor.from_user(user)

        print("")
        closed = apply_repair(db, findings, since=since, until=args.until, actor=actor)
        print(f"closed {len(closed)} onboarding cycle(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
