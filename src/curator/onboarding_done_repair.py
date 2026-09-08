"""Close the onboarding cycles that reached «Завершено» and stayed open anyway.

``done`` used to be a column rather than a decision. Marking a card «Завершено» set the
status and nothing else: the cycle stayed open until the *relationship* ended, so the card sat
in the last column of its curator's board for weeks or months, and the board's own «Завершено»
became an in-tray nobody could empty. **312 open ``done`` cards** were sitting there when the
owner settled it — see :func:`~src.curator.onboarding_core.set_status`, where reaching ``done``
now closes the cycle. That fixes every card from here on; this command is for the 312.

**Only rows that are genuinely finished and genuinely open.** Every condition is a reason to
leave a row alone if it does not hold:

``close``
    Open (``ended_at IS NULL``), ``status = 'done'``, and completed by a person
    (``completed_by`` is set). That conjunction is a card a curator finished — exactly what
    the new rule would have closed at the moment they finished it.

``skipped: already closed``
    ``ended_at`` is set. This is what makes a second run a no-op, and it is the honest answer
    for every card closed since the rule shipped.

``skipped: not done``
    ``new``, ``in_progress`` or ``cancelled``. Still somebody's work, or already history.

``skipped: launch baseline``
    ``done`` with no ``completed_by`` — the synthetic rows the launch backfill seeded so the
    board would start clean. Nobody completed them, they are already hidden from every board
    (see :func:`~src.curator.onboarding_core.board_query`), and closing them would rewrite the
    history of a relationship that never had an onboarding, for no visible gain. They are also
    the rows most likely to name a group the student has long since left, which is the one
    shape that can make the reconciler open a fresh cycle — see below.

**What closing does downstream.** Closing frees the pair's one open-cycle slot, so the next
reconciler sweep asks whether to open a new one.
:func:`~src.curator.onboarding_core.already_onboarded_into_group` refuses when the most recent
closed cycle is ``completed`` on the same group, which is the normal case: the reconciler keeps
each open card's ``group_id`` pointed at the student's current group, so a card closed today
names the group they are in today. The ``reopens`` column reports the exception — a pair whose
live group is *not* the one on the card, where a fresh card is the intended behaviour
("finishing one course and starting another is a real onboarding") but is still a card
appearing in «Новые» tomorrow that somebody should be expecting. **Read that number before
applying.**

``end_reason`` is :data:`~src.curator.onboarding_core.END_COMPLETED`, the same reason
``set_status`` now writes, so the repaired rows and the ones closed from here on are
indistinguishable to every reader — including the re-open guard, which needs to see it.

``ended_at`` is the moment the repair runs, not ``completed_at``. The cycle ended when it was
closed, and that is today; ``completed_at`` already records when the curator finished, and
back-dating one to the other would lose that distinction for no gain.

**Audited through the domain, not around it.** Closing goes through
:func:`~src.curator.onboarding_core.close_cycle`, so each card gets the same ``cycle.closed``
history row every other close writes, attributed to whoever ran the command.

**Idempotent, and re-checked at the moment of writing.** The scan may be minutes old by the
time somebody reads the table and types ``--apply``; every condition is evaluated again per
row inside the apply loop, so nothing is closed on the strength of the report alone.

Expected shape of a dry run on production, from the numbers the owner was given: **312**
``close`` rows, plus whatever the launch backfill left as ``skipped: launch baseline``. A run
reporting substantially more than 312 closes means something other than this backlog has
been swept in — read the difference before applying. Do **not** run it against production
casually; it is written to be read first.

Usage::

    python -m src.curator.onboarding_done_repair                    # dry run, prints a table
    python -m src.curator.onboarding_done_repair --json
    python -m src.curator.onboarding_done_repair --curator-id 42    # one curator's board
    python -m src.curator.onboarding_done_repair --apply --actor-email admin@example.com
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
    END_COMPLETED,
    STATUS_DONE,
    OnboardingActor,
    close_cycle,
    compute_active_pairs,
)
from src.schemas.models import CuratorOnboarding, Group, UserInDB

logger = logging.getLogger(__name__)

#: Bumped when the eligibility rules change, so a report says which rules produced it.
REPAIR_VERSION = "onboarding-done-repair-1"

VERDICT_CLOSE = "close"
VERDICT_ALREADY_CLOSED = "skipped: already closed"
VERDICT_NOT_DONE = "skipped: not done"
VERDICT_BASELINE = "skipped: launch baseline"


@dataclass
class Finding:
    """One ``done`` card, the evidence about it, and the verdict."""

    onboarding_id: int
    curator_id: int
    curator_name: str
    student_id: int
    group_id: Optional[int]
    group_name: str
    cycle_no: int
    status: str
    completed_at: Optional[str]
    completed_by: Optional[int]
    ended_at: Optional[str]
    #: True when the pair is still live, i.e. the reconciler will consider them tomorrow.
    pair_is_live: bool
    #: The group the pair is live on today, when that is not the group on the card.
    live_group_id: Optional[int]
    #: True when closing this row would let the reconciler open a fresh cycle — the pair is
    #: live on a *different* group, which the re-open guard reads as a new onboarding.
    reopens: bool
    verdict: str

    @property
    def is_actionable(self) -> bool:
        return self.verdict == VERDICT_CLOSE


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _verdict(row: CuratorOnboarding) -> str:
    """Ordered most-specific first: the first true reason is the one printed."""
    if row.ended_at is not None:
        return VERDICT_ALREADY_CLOSED
    if row.status != STATUS_DONE:
        return VERDICT_NOT_DONE
    if row.completed_by is None:
        return VERDICT_BASELINE
    return VERDICT_CLOSE


def scan(
    db: Session,
    *,
    curator_id: Optional[int] = None,
    only_ids: Optional[Iterable[int]] = None,
    active: Optional[dict[tuple[int, int], int]] = None,
) -> list[Finding]:
    """Every ``done`` card, classified. Writes nothing.

    ``done`` is the only thing narrowed in SQL — the rest is evaluated in Python and reported,
    because a row that *looks* like a candidate and is skipped is the most useful line in the
    table: it is the one a human has to agree with. ``only_ids`` re-classifies specific cards
    with the identical rules, which is what the apply loop uses so the check guarding the
    write cannot drift from the check that produced the report.

    ``active`` is the live-relationship map, computed here when not supplied. The apply loop
    passes the one it already has: it is an organisation-wide read, it feeds only the
    ``reopens`` *column* and never the verdict, and re-deriving it once per card would make a
    312-row repair hundreds of full sweeps.
    """
    if only_ids is not None:
        ids = [int(i) for i in only_ids]
        if not ids:
            return []
        # Not filtered by status: a card that has moved off ``done`` since the scan must still
        # come back, so the apply loop sees "not done" rather than an empty result it would
        # read as "row is gone".
        query = db.query(CuratorOnboarding).filter(CuratorOnboarding.id.in_(ids))
    else:
        query = db.query(CuratorOnboarding).filter(CuratorOnboarding.status == STATUS_DONE)
    if curator_id is not None:
        query = query.filter(CuratorOnboarding.curator_id == int(curator_id))
    rows = query.order_by(CuratorOnboarding.curator_id.asc(), CuratorOnboarding.id.asc()).all()
    if not rows:
        return []

    # The same reading the reconciler will do tomorrow, so the `reopens` column is a
    # prediction made with the reconciler's own eyes rather than a guess.
    if active is None:
        active = compute_active_pairs(db)
    group_ids = {int(r.group_id) for r in rows if r.group_id is not None}
    group_ids |= {int(g) for g in active.values()}
    group_names = {
        int(g.id): (g.name or "").strip()
        for g in db.query(Group).filter(Group.id.in_(group_ids)).all()
    }
    curator_names = {
        int(u.id): (getattr(u, "official_full_name", None) or u.name or "")
        for u in db.query(UserInDB)
        .filter(UserInDB.id.in_({int(r.curator_id) for r in rows}))
        .all()
    }

    findings: list[Finding] = []
    for row in rows:
        group_id = int(row.group_id) if row.group_id is not None else None
        live_group = active.get((int(row.curator_id), int(row.student_id)))
        live_group = int(live_group) if live_group is not None else None
        verdict = _verdict(row)
        findings.append(
            Finding(
                onboarding_id=int(row.id),
                curator_id=int(row.curator_id),
                curator_name=curator_names.get(int(row.curator_id), f"#{row.curator_id}"),
                student_id=int(row.student_id),
                group_id=group_id,
                group_name=(
                    group_names.get(group_id) or (f"#{group_id}" if group_id else "—")
                ),
                cycle_no=int(row.cycle_no or 1),
                status=row.status,
                completed_at=_iso(row.completed_at),
                completed_by=row.completed_by,
                ended_at=_iso(row.ended_at),
                pair_is_live=live_group is not None,
                live_group_id=live_group,
                reopens=(
                    verdict == VERDICT_CLOSE
                    and live_group is not None
                    and live_group != group_id
                ),
                verdict=verdict,
            )
        )
    return findings


def scan_one(
    db: Session,
    onboarding_id: int,
    *,
    active: Optional[dict[tuple[int, int], int]] = None,
) -> Optional[Finding]:
    """Re-classify a single card with the identical rules :func:`scan` uses."""
    found = scan(db, only_ids=[int(onboarding_id)], active=active)
    return found[0] if found else None


def apply_repair(
    db: Session,
    findings: list[Finding],
    *,
    actor: Optional[OnboardingActor] = None,
) -> list[Finding]:
    """Close the cards the scan cleared, one committed transaction each.

    Per-card rather than one transaction for all of them: these are hundreds of unrelated
    students, and one row that fails must not roll back the rest.
    """
    actor = actor or OnboardingActor.system("исправление онбординга")
    closed: list[Finding] = []
    if not any(f.is_actionable for f in findings):
        return closed
    active = compute_active_pairs(db)

    for finding in findings:
        if not finding.is_actionable:
            continue

        # Re-read and re-decide. Between the scan and this line a curator may have moved the
        # card, or the reconciler may have closed it.
        fresh = scan_one(db, finding.onboarding_id, active=active)
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
            if not close_cycle(db, row, END_COMPLETED, actor):
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


def _summary(findings: list[Finding]) -> dict:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    closable = [f for f in findings if f.verdict == VERDICT_CLOSE]
    return {
        "repair_version": REPAIR_VERSION,
        "generated_at": datetime.utcnow().isoformat(),
        "done_cards": len(findings),
        "would_close": len(closable),
        "would_reopen": sum(1 for f in closable if f.reopens),
        "curators": len({f.curator_id for f in closable}),
        "by_verdict": dict(sorted(counts.items())),
    }


def render_table(findings: list[Finding]) -> str:
    """The report a human reads before typing ``--apply``.

    Every column is evidence for the verdict in the last one: who completed the card and when
    (a card nobody completed is a launch seed, not an achievement), whether the pair is still
    live, and whether closing it would hand the reconciler a fresh card tomorrow.
    """
    headers = [
        "card", "curator", "student", "group", "cycle",
        "status", "completed", "by", "live", "reopens", "verdict",
    ]
    rows = [
        [
            str(f.onboarding_id),
            f"{f.curator_id} {f.curator_name}".strip(),
            str(f.student_id),
            f"{f.group_id or '—'} {f.group_name}".strip(),
            str(f.cycle_no),
            f.status,
            (f.completed_at or "—")[:19],
            str(f.completed_by or "—"),
            (str(f.live_group_id) if f.pair_is_live else "no"),
            "YES" if f.reopens else "no",
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
    lines += ["", f"done cards: {summary['done_cards']}"]
    for verdict, count in summary["by_verdict"].items():
        lines.append(f"  {verdict}: {count}")
    if summary["would_close"]:
        lines += [
            "",
            f"would close {summary['would_close']} card(s) across "
            f"{summary['curators']} curator(s).",
        ]
    if summary["would_reopen"]:
        lines += [
            "",
            f"! {summary['would_reopen']} of them (the «reopens: YES» rows) name a group the "
            "student has moved on from.",
            "  Closing frees the pair's slot and the next sweep will open a *new* card for "
            "the group they are in now —",
            "  which is the intended rule for starting a second course, but it is a card "
            "appearing in «Новые» tomorrow.",
        ]
    if not summary["would_close"]:
        lines += ["", "nothing to close — every done card already has a reason to stay."]
    return "\n".join(lines)


def _report_json(findings: list[Finding]) -> str:
    return json.dumps(
        {**_summary(findings), "findings": [asdict(f) for f in findings]},
        indent=2,
        ensure_ascii=False,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Close the onboarding cycles that reached «Завершено» and stayed open. "
            "Dry run by default."
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
        "--curator-id", type=int, default=None, help="restrict to one curator's board"
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
        findings = scan(db, curator_id=args.curator_id)
        print(_report_json(findings) if args.json else render_table(findings))

        if not args.apply:
            if not args.json:
                print("\nDRY RUN — nothing was written. Re-run with --apply to close them.")
            return 0

        actor = None
        if args.actor_email:
            user = db.query(UserInDB).filter(UserInDB.email == args.actor_email).first()
            if user is None:
                print(f"no LMS user with email {args.actor_email}", file=sys.stderr)
                return 2
            actor = OnboardingActor.from_user(user)

        print("")
        closed = apply_repair(db, findings, actor=actor)
        print(f"closed {len(closed)} onboarding cycle(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
