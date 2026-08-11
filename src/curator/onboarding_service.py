"""
Onboarding reconciler.

Derives one onboarding card per active (curator, student) pairing. Runs on a
background thread (startup + hourly), replacing the paused curator task scheduler.
Detection is relationship-based, not timestamp-based, so it handles late curator
assignment and transfers correctly.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from src.config import SessionLocal
from src.schemas.models import (
    CuratorOnboarding, UserInDB, Group, GroupStudent,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("new", "in_progress")


def telegram_link(tg: Optional[str]) -> Optional[str]:
    if not tg:
        return None
    handle = tg.strip().lstrip("@")
    if not handle or handle.isdigit():
        return None
    return f"https://t.me/{handle}"


def compute_active_pairs(db) -> dict:
    """{(curator_id, student_id): group_id} for active groups with a curator."""
    rows = (
        db.query(Group.curator_id, GroupStudent.student_id,
                 GroupStudent.group_id, GroupStudent.created_at)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .join(UserInDB, UserInDB.id == GroupStudent.student_id)
        .filter(Group.is_active == True, Group.curator_id.isnot(None),
                UserInDB.is_active == True)
        .all()
    )
    pairs: dict = {}
    seen_created: dict = {}
    for curator_id, student_id, group_id, created_at in rows:
        key = (curator_id, student_id)
        ts = created_at or datetime.min
        if key not in pairs or ts > seen_created[key]:
            pairs[key] = group_id
            seen_created[key] = ts
    return pairs


def reconcile_onboarding(db) -> dict:
    active = compute_active_pairs(db)
    existing = {(r.curator_id, r.student_id): r
                for r in db.query(CuratorOnboarding).all()}

    created = reactivated = cancelled = 0

    # add / reactivate
    for (curator_id, student_id), group_id in active.items():
        row = existing.get((curator_id, student_id))
        if row is None:
            db.add(CuratorOnboarding(
                curator_id=curator_id, student_id=student_id,
                group_id=group_id, status="new",
            ))
            created += 1
        elif row.status == "cancelled":
            row.status = "new"
            row.group_id = group_id
            reactivated += 1
        elif row.status in ACTIVE_STATUSES:
            row.group_id = group_id  # keep display group fresh

    # retire pairs that are no longer active (but leave 'done' history alone)
    for key, row in existing.items():
        if key not in active and row.status in ACTIVE_STATUSES:
            row.status = "cancelled"
            cancelled += 1

    db.commit()
    return {"created": created, "reactivated": reactivated, "cancelled": cancelled}


class OnboardingReconciler:
    def __init__(self, check_interval: int = 3600):
        self.check_interval = check_interval
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("Onboarding reconciler started")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        self._tick()  # startup run
        while self.running:
            time.sleep(self.check_interval)
            if not self.running:
                break
            self._tick()

    def _tick(self):
        db = SessionLocal()
        try:
            result = reconcile_onboarding(db)
            if result["created"] or result["reactivated"] or result["cancelled"]:
                logger.info(f"[ONBOARDING] reconcile {result}")
        except Exception as e:
            logger.error(f"[ONBOARDING] reconcile error: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()


_reconciler: Optional[OnboardingReconciler] = None


def start_onboarding_reconciler():
    global _reconciler
    if _reconciler is None:
        _reconciler = OnboardingReconciler(check_interval=3600)
    _reconciler.start()
