"""Bookkeeping loop: flip trial_accesses active→expired past deadline.

Enforcement is request-time (src/trials/services.grant_is_active); this job only
keeps list views / audit truthful. Runs ONLY in the scheduler container
(started from run_scheduler.py) so API workers never double-run it.
"""
import logging
import threading
import time

from src.config import SessionLocal  # same import as curator_task_scheduler
from src.trials.services import expire_stale_trials

logger = logging.getLogger(__name__)


def run_once() -> int:
    db = SessionLocal()
    try:
        count = expire_stale_trials(db)
        if count:
            logger.info("[TRIALS] marked %s trial grant(s) expired", count)
        return count
    finally:
        db.close()


class TrialStatusScheduler:
    def __init__(self, check_interval: int = 300):
        self.check_interval = check_interval
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="trial-status")
        self.thread.start()
        logger.info("Trial status scheduler started (interval: %ss)", self.check_interval)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def _run(self):
        while self.running:
            try:
                run_once()
            except Exception as e:  # never die on a tick
                logger.error("[TRIALS] status tick failed: %s", e, exc_info=True)
            time.sleep(self.check_interval)
