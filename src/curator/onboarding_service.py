"""Onboarding reconciler thread.

The domain rules moved to :mod:`src.curator.onboarding_core` when the CRM became the
canonical onboarding UI — both callers must agree on what a cycle is, so neither owns the
logic. This module is now just the scheduling shell around
:func:`~src.curator.onboarding_core.reconcile_onboarding`, plus the re-exports the rest of
the codebase already imports from here.
"""
import logging
import threading
import time
from typing import Optional

from src.config import SessionLocal
from src.curator.onboarding_core import (  # noqa: F401 - re-exported for existing importers
    ACTIVE_STATUSES,
    compute_active_pairs,
    reconcile_onboarding,
    telegram_link,
)

logger = logging.getLogger(__name__)


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
            if any(result.values()):
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
