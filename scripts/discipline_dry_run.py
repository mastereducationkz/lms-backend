"""Read-only: print the teacher discipline register for a period, as the API would return it.

Run it before anybody is fined, and check two or three teachers by eye against the Meet attendance
page. Writes nothing: the transaction is read-only and rolled back.

    ssh root@188.130.160.227 'docker exec -i -w /app -e PYTHONPATH=/app lms-scheduler python -' \
        < scripts/discipline_dry_run.py
"""
import os
from datetime import date

from sqlalchemy import text

from src.config import SessionLocal
from src.discipline import service
from src.discipline.rules import RULE_START, period_containing

period_key = os.environ.get("PERIOD", RULE_START.isoformat())
period = period_containing(date.fromisoformat(period_key))
if period is None:
    raise SystemExit(f"the register starts on {RULE_START.strftime('%d.%m.%Y')}")

db = SessionLocal()
try:
    db.execute(text("SET TRANSACTION READ ONLY"))
    register = service.register(db, period)
    totals = register["totals"]
    print(f"{register['period']['label']} — {totals['lessons']} lessons, "
          f"{totals['unmeasurable']} without a Meet room")
    print(f"late {totals['late_minutes']} min · short {totals['early_minutes']} min · "
          f"missed {totals['misses']} · proposed {totals['fine']:,} ₸"
          + (f" · {totals['unpriced']} still to price" if totals['unpriced'] else ""))
    print()
    print(f"{'teacher':34s} {'prog':6s} {'lessons':>7s} {'late':>5s} {'short':>6s} {'miss':>5s} {'₸':>9s}")
    for row in sorted(register["teachers"], key=lambda r: -r["totals"]["fine"]):
        t = row["totals"]
        if not (t["late_minutes"] or t["early_minutes"] or t["misses"]):
            continue
        print(f"{row['name'][:34]:34s} {row['program'][:6]:6s} {t['lessons']:7d} "
              f"{t['late_minutes']:5d} {t['early_minutes']:6d} {t['misses']:5d} {t['fine']:9,d}")
    print()
    for row in sorted(register["teachers"], key=lambda r: -r["totals"]["fine"])[:3]:
        for day, cell in sorted(row["days"].items()):
            if cell["state"] in ("late", "miss", "ended_early"):
                detail = service.day_detail(db, row["teacher_id"], date.fromisoformat(day))
                for lesson in detail["lessons"]:
                    for finding in lesson["findings"]:
                        print(f"{day} {row['name'][:22]:22s} {lesson['group'][:26]:26s} "
                              f"{lesson['starts_at'][11:16]}–{lesson['ends_at'][11:16]}Z "
                              f"joined {(lesson['first_join'] or '—')[11:16]} "
                              f"{finding['kind']} {finding['minutes']} min"
                              f"{' (made up)' if finding['made_up'] else ''}")
finally:
    db.rollback()
    db.close()
