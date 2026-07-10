"""Reconcile LMS group memberships against a target platform's students — find who to provision.

The cross-platform sync (SSO_SYNC_DESIGN.md) can only attach a student to a SAT/NUET/IELTS group if
that student already has an account on the target (matched by email). This tool lists the students who
are in a SAT/NUET/IELTS group in the LMS but are **not** on the target, and classifies each gap:

  * ``absent``          — no account on the target at all → provision them.
  * ``email_mismatch``  — a same-NAME account exists on the target under a DIFFERENT email → fix the email
                          (their membership will then sync). The matched target email is included.

By default only **current** memberships are considered (finished ``is_over`` groups and inactive students
are excluded — pass ``--include-finished`` / ``--include-inactive`` to see them). After the team provisions
the ``absent`` students / fixes the ``email_mismatch`` emails, re-run ``sync_backfill`` (idempotent) to sync.

Usage (run where the LMS DB is reachable):
    # 1. export the target platform's students (email,name), e.g. on the VM for IELTS:
    #    docker exec ielts-db-1 psql -U <u> -d <d> -tAc \
    #      "\\copy (SELECT email, full_name FROM users WHERE email IS NOT NULL) TO STDOUT WITH CSV" > ielts_users.csv
    # 2. reconcile:
    python -m src.services.sync_reconcile --platform ielts --target-users ielts_users.csv -o ielts_gaps.csv
    python -m src.services.sync_reconcile --platform sat,nuet --target-users sat_users.csv -o sat_gaps.csv
    # (without --target-users, every out-of-sync student is reported as 'absent' — no mismatch detection.)
"""

from __future__ import annotations

import argparse
import csv
import sys

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


def _norm_name(name: str | None) -> str:
    """Order-insensitive, case-insensitive name key ('Aizhan Bekova' == 'bekova  aizhan')."""
    return " ".join(sorted((name or "").lower().split()))


_MEMBERS_SQL = text(
    """
    SELECT DISTINCT lower(u.email) AS email, u.name AS name, u.is_active AS student_active,
           g.name AS group_name, g.program_type AS program, g.is_over AS is_over
    FROM group_students gs
    JOIN users u  ON u.id  = gs.student_id
    JOIN groups g ON g.id  = gs.group_id
    WHERE g.program_type IN :programs
      AND u.email IS NOT NULL AND u.email <> ''
    """
).bindparams(bindparam("programs", expanding=True))


def load_target_users(path: str) -> tuple[set[str], dict[str, str]]:
    """Read a CSV/TSV of the target platform's (email, name). Returns (emails, name_key -> email)."""
    emails: set[str] = set()
    name_to_email: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        sniff = f.read(4096)
        f.seek(0)
        delim = "\t" if "\t" in sniff and "," not in sniff.split("\n")[0] else ","
        for row in csv.reader(f, delimiter=delim):
            if not row or not row[0].strip():
                continue
            email = row[0].strip().lower()
            name = row[1].strip() if len(row) > 1 else ""
            emails.add(email)
            key = _norm_name(name)
            if key and key not in name_to_email:
                name_to_email[key] = email
    return emails, name_to_email


def reconcile(
    db: Session,
    *,
    programs: list[str],
    target_emails: set[str] | None = None,
    target_name_to_email: dict[str, str] | None = None,
    include_finished: bool = False,
    include_inactive: bool = False,
) -> list[dict]:
    """Return one row per (student, gap) — students in the given programs' groups missing from the target."""
    target_emails = target_emails or set()
    target_name_to_email = target_name_to_email or {}
    rows = db.execute(_MEMBERS_SQL, {"programs": programs}).mappings().all()

    seen: dict[str, dict] = {}  # de-dupe by email; keep the "most current" membership context
    for r in rows:
        email = r["email"]
        if target_emails and email in target_emails:
            continue  # already on the target → synced, not a gap
        if not include_finished and r["is_over"]:
            continue
        if not include_inactive and not r["student_active"]:
            continue
        if target_emails:
            match = target_name_to_email.get(_norm_name(r["name"]))
            classification = "email_mismatch" if match else "absent"
        else:
            classification = "unknown (no target export)"
            match = None
        out = {
            "email": email,
            "name": r["name"],
            "program": r["program"],
            "group_name": r["group_name"],
            "is_over_group": r["is_over"],
            "student_active": r["student_active"],
            "classification": classification,
            "target_email_if_mismatch": match or "",
        }
        # prefer a current (not-over) group's context if the student appears in several
        prev = seen.get(email)
        if prev is None or (prev["is_over_group"] and not r["is_over"]):
            seen[email] = out
    return sorted(seen.values(), key=lambda d: (d["classification"], d["program"], d["email"]))


def main() -> None:  # pragma: no cover - CLI
    p = argparse.ArgumentParser(description="Reconcile LMS memberships vs a target platform's students.")
    p.add_argument("--platform", required=True, help="comma-separated program(s): sat,nuet,ielts")
    p.add_argument("--target-users", help="CSV/TSV of the target platform's (email,name); omit => all 'absent'")
    p.add_argument("--include-finished", action="store_true", help="include finished (is_over) groups")
    p.add_argument("--include-inactive", action="store_true", help="include inactive students")
    p.add_argument("-o", "--out", help="output CSV path (default: stdout)")
    args = p.parse_args()

    programs = [x.strip().lower() for x in args.platform.split(",") if x.strip()]
    target_emails, name_map = (load_target_users(args.target_users) if args.target_users else (None, None))

    from src.config import SessionLocal

    db = SessionLocal()
    try:
        gaps = reconcile(db, programs=programs, target_emails=target_emails,
                         target_name_to_email=name_map, include_finished=args.include_finished,
                         include_inactive=args.include_inactive)
    finally:
        db.close()

    fields = ["email", "name", "program", "group_name", "is_over_group", "student_active",
              "classification", "target_email_if_mismatch"]
    fh = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(gaps)
    finally:
        if args.out:
            fh.close()

    counts: dict[str, int] = {}
    for g in gaps:
        counts[g["classification"]] = counts.get(g["classification"], 0) + 1
    print(f"reconcile {programs}: {len(gaps)} students to act on -> {counts}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    main()
