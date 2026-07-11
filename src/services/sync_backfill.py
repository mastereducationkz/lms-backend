"""One-time backfill for cross-platform sync (SSO_SYNC_DESIGN.md §9-11).

The p7/p8 DB triggers are CDC — they enqueue only *changes* from the moment they were installed.
Existing groups and memberships are therefore invisible to the targets until they next change.
This backfill enqueues the *current* state as idempotent ``group.upserted`` / ``member.upserted``
rows in ``student_sync_outbox`` — the exact payload shape the triggers produce (only ``source`` differs:
``lms_backfill``) — so the drainer delivers them and each target upserts to its current state.

Idempotent by construction: the consumers upsert, so re-running just re-delivers (harmless, wasteful).
Run it ONCE, ideally AFTER the target consumers are enabled, so it flushes in a single clean pass:

    docker compose exec backend python -m src.services.sync_backfill --members --groups
    docker compose exec backend python -m src.services.sync_backfill --members --dry-run   # counts only

Postgres-only (json_build_object / gen_random_uuid, like the triggers).
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# group.upserted for every group in the given programs (SAT/NUET have group entities; IELTS
# creates groups implicitly from membership, so it is not a group-entity target).
_GROUPS_SQL = text(
    """
    WITH g AS (
        SELECT grp.id, grp.name, grp.program_type, grp.is_active,
               t.email AS teacher_email, t.name AS teacher_name,
               c.email AS curator_email, c.name AS curator_name,
               gen_random_uuid()::text AS eid
        FROM groups grp
        LEFT JOIN users t ON t.id = grp.teacher_id
        LEFT JOIN users c ON c.id = grp.curator_id
        WHERE grp.program_type IN :programs
          -- Only backfill groups that actually have members — a member-less LMS group has no
          -- reason to exist on any platform, and emitting it would manufacture an empty group.
          AND EXISTS (SELECT 1 FROM group_students gs WHERE gs.group_id = grp.id)
    )
    INSERT INTO student_sync_outbox (event_id, event_type, payload, status, attempts, created_at)
    SELECT g.eid, 'group.upserted',
        json_build_object(
            'event_id', g.eid,
            'event_type', 'group.upserted',
            'source', 'lms_backfill',
            'group', json_build_object(
                'lms_group_id', g.id,
                'name', g.name,
                'program_type', g.program_type,
                'is_active', COALESCE(g.is_active, true),
                'teacher_email', g.teacher_email,
                'teacher_name', g.teacher_name,
                'curator_email', g.curator_email,
                'curator_name', g.curator_name
            )
        ),
        'pending', 0, (now() AT TIME ZONE 'utc')
    FROM g
    """
).bindparams(bindparam("programs", expanding=True))

# member.upserted for every group_students row whose group is in the given programs.
_MEMBERS_SQL = text(
    """
    WITH m AS (
        SELECT gs.group_id, gs.student_id, u.email, u.central_auth_user_id, u.name AS uname,
               gr.program_type, gr.name AS gname, gen_random_uuid()::text AS eid
        FROM group_students gs
        JOIN users u  ON u.id  = gs.student_id
        JOIN groups gr ON gr.id = gs.group_id
        WHERE gr.program_type IN :programs
    )
    INSERT INTO student_sync_outbox (event_id, event_type, payload, status, attempts, created_at)
    SELECT m.eid, 'member.upserted',
        json_build_object(
            'event_id', m.eid,
            'event_type', 'member.upserted',
            'source', 'lms_backfill',
            'lms_group_id', m.group_id,
            'group_name', m.gname,
            'program_type', m.program_type,
            'student', json_build_object(
                'lms_student_id', m.student_id,
                'email', m.email,
                'central_auth_user_id', m.central_auth_user_id,
                'name', m.uname
            )
        ),
        'pending', 0, (now() AT TIME ZONE 'utc')
    FROM m
    """
).bindparams(bindparam("programs", expanding=True))

_COUNT_GROUPS = text("SELECT count(*) FROM groups WHERE program_type IN :programs").bindparams(
    bindparam("programs", expanding=True)
)
_COUNT_MEMBERS = text(
    """
    SELECT count(*) FROM group_students gs JOIN groups gr ON gr.id = gs.group_id
    WHERE gr.program_type IN :programs
    """
).bindparams(bindparam("programs", expanding=True))

# SAT/NUET consume groups; members go to SAT/NUET + IELTS. general_english has no target.
# IELTS now consumes group.upserted too (for the group's teacher/curator), so it is included
# in the group backfill — SAT ignores ielts-program group events; IELTS ignores sat/nuet.
GROUP_PROGRAMS = ["sat", "nuet", "ielts"]
MEMBER_PROGRAMS = ["sat", "nuet", "ielts"]


def backfill(
    db: Session,
    *,
    do_groups: bool = False,
    do_members: bool = False,
    group_programs: list[str] | None = None,
    member_programs: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Enqueue current groups/memberships. Returns {groups, members} counts enqueued (or would-be)."""
    group_programs = group_programs or GROUP_PROGRAMS
    member_programs = member_programs or MEMBER_PROGRAMS
    result = {"groups": 0, "members": 0, "dry_run": dry_run}

    if do_groups:
        if dry_run:
            result["groups"] = db.execute(_COUNT_GROUPS, {"programs": group_programs}).scalar() or 0
        else:
            result["groups"] = db.execute(_GROUPS_SQL, {"programs": group_programs}).rowcount

    if do_members:
        if dry_run:
            result["members"] = db.execute(_COUNT_MEMBERS, {"programs": member_programs}).scalar() or 0
        else:
            result["members"] = db.execute(_MEMBERS_SQL, {"programs": member_programs}).rowcount

    if dry_run:
        db.rollback()
    else:
        db.commit()
    logger.info("sync backfill: %s", result)
    return result


def main() -> None:  # pragma: no cover - CLI entrypoint
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Backfill cross-platform sync outbox from current state.")
    parser.add_argument("--groups", action="store_true", help="enqueue group.upserted for SAT/NUET groups")
    parser.add_argument("--members", action="store_true", help="enqueue member.upserted for SAT/NUET/IELTS members")
    parser.add_argument("--programs", help="override programs (comma-separated) for BOTH groups and members")
    parser.add_argument("--dry-run", action="store_true", help="count only; enqueue nothing")
    args = parser.parse_args()
    if not args.groups and not args.members:
        parser.error("pass --groups and/or --members")

    from src.config import SessionLocal

    programs = [p.strip().lower() for p in args.programs.split(",")] if args.programs else None
    db = SessionLocal()
    try:
        res = backfill(
            db,
            do_groups=args.groups,
            do_members=args.members,
            group_programs=[p for p in programs if p in ("sat", "nuet")] if programs else None,
            member_programs=programs,
            dry_run=args.dry_run,
        )
        print(f"backfill result: {res}")
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
