#!/usr/bin/env python3
"""Record the hellos staff already sent by hand, so the automatic hello never repeats one.

The hello went out as Support announcements (#4, #5, #7, #8, #9, #11 on 12–14 Sep; #14–#16 on
2026-09-15). Their recipients are Support group ids, which is what this reads — one or more per
line, or separated by spaces/commas — on stdin. Each id linked to an LMS group gets a
``telegram_group_greetings`` row (``source="backfill"``, ``status="sent"``); existing rows are left
as they are. Ids that are not linked are listed and skipped.

Usage (inside the backend container)::

    echo "27 36 42 …" | python scripts/backfill_group_greetings.py [--dry-run]
"""
import json
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.config import SessionLocal  # noqa: E402
import src.models  # noqa: E402,F401 — the whole metadata, as the app sees it
from src.announcements.models import TelegramGroupGreeting, TelegramGroupLink  # noqa: E402
from src.schemas.models import Group  # noqa: E402
from src.services.group_bot_hello import variant_for  # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    ids = sorted({int(token) for token in re.findall(r"\d+", sys.stdin.read())})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    report = {"given": len(ids), "inserted": [], "already_greeted": [], "not_linked": []}
    try:
        links = {link.support_group_id: link for link in
                 db.query(TelegramGroupLink).filter(TelegramGroupLink.support_group_id.in_(ids)).all()}
        for support_group_id in ids:
            link = links.get(support_group_id)
            if link is None:
                report["not_linked"].append(support_group_id)
                continue
            existing = (db.query(TelegramGroupGreeting)
                        .filter(TelegramGroupGreeting.lms_group_id == link.lms_group_id).first())
            if existing is not None:
                report["already_greeted"].append(support_group_id)
                continue
            group = db.get(Group, link.lms_group_id)
            db.add(TelegramGroupGreeting(lms_group_id=link.lms_group_id, support_group_id=support_group_id,
                                         variant=variant_for(group), source="backfill", status="sent",
                                         attempts=0, created_at=now, sent_at=now))
            report["inserted"].append(support_group_id)
        if dry_run:
            db.rollback()
        else:
            db.commit()
    finally:
        db.close()
    report["dry_run"] = dry_run
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
