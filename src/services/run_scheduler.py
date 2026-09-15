#!/usr/bin/env python3
"""
Standalone Lesson Reminder Scheduler Runner
Runs the scheduler in a separate process/container
"""
import logging
import signal
import time
import sys
import os

# Setup logging before any imports
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Import after logging setup
from src.services.lesson_reminder_scheduler import LessonReminderScheduler
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def _stop_on_sigterm(signum, frame):
    """Take the graceful path below on SIGTERM, as on Ctrl+C.

    A deploy (``docker compose up -d``) and ``docker stop`` send SIGTERM. Running as PID 1 with no handler,
    the process ignored it and was killed ten seconds later — mid-recording, with the attempt already
    counted (2026-09-15). Now it gives back what it cut off before it goes.
    """
    raise KeyboardInterrupt


def main():
    """Main scheduler runner"""
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    logger.info("=" * 80)
    logger.info("🚀 STARTING LESSON REMINDER SCHEDULER")
    logger.info("=" * 80)

    # Start the self-hosted video ingest worker (YouTube -> HLS -> S3). It runs
    # only here (scheduler container) so the API process never double-processes
    # jobs. Independent of the email config below.
    video_worker = None
    if os.getenv('ENABLE_VIDEO_INGEST', 'true').lower() == 'true':
        try:
            from src.services.video_ingest import VideoIngestWorker
            video_worker = VideoIngestWorker(poll_interval=int(os.getenv('VIDEO_INGEST_POLL', '15')))
            video_worker.start()
        except Exception as e:
            logger.error(f"Failed to start video ingest worker: {e}", exc_info=True)

    # Start the Meet closer independently so recording conversion/transcription
    # cannot postpone the 15-minute room-close policy.
    room_closer_worker = None
    try:
        from src.services.meet_room_closer import MeetRoomCloserWorker
        room_closer_worker = MeetRoomCloserWorker(
            poll_interval=os.getenv('MEET_ROOM_CLOSER_POLL_SECONDS', '60')
        )
        room_closer_worker.start()
    except Exception as e:
        logger.error(f"Failed to start Meet room closer: {e}", exc_info=True)

    # Per-group Google Calendars (create, share read-only, sync within a minute). Its own
    # thread so a slow Google call never delays reminders; no-op unless ENABLE_GROUP_CALENDARS.
    try:
        from src.services.group_calendar import GroupCalendarWorker
        GroupCalendarWorker(poll_interval=int(os.getenv('GROUP_CALENDAR_POLL_SECONDS', '60'))).start()
    except Exception as e:
        logger.error(f"Failed to start group calendars worker: {e}", exc_info=True)

    # Start the lesson-recording pipeline (Meet -> Drive -> HLS -> S3). Scheduler
    # container only, same as video ingest, so the API process never double-processes.
    # Self-gating: both workers return immediately unless ENABLE_RECORDINGS
    # is true AND the OAuth env is complete.
    recordings_worker = ingest_worker = None
    try:
        from src.services.recordings_worker import RecordingIngestWorker, RecordingsWorker
        # Recordings are made watchable on a thread of their own, one after another, instead of
        # waiting for the tick's other steps and its five-minute sleep (2026-09-15).
        ingest_worker = RecordingIngestWorker()
        ingest_worker.start()
        recordings_worker = RecordingsWorker(
            poll_interval=int(os.getenv('RECORDINGS_POLL_SECONDS', '300')),
            ingest_in_tick=not ingest_worker.running,
        )
        recordings_worker.start()
    except Exception as e:
        logger.error(f"Failed to start recordings worker: {e}", exc_info=True)

    # Start the cross-platform sync outbox drainer (HTTP-pushes group/membership changes to
    # SAT/NUET, later IELTS). No-op unless SYNC_ENABLED; runs only here so the API process
    # never double-drains. See SSO_SYNC_DESIGN.md.
    try:
        import threading
        from src.services.student_sync import run_drain_loop
        threading.Thread(
            target=run_drain_loop,
            kwargs={"poll_seconds": int(os.getenv("SYNC_POLL_SECONDS", "15"))},
            daemon=True,
            name="student-sync-drainer",
        ).start()
    except Exception as e:
        logger.error(f"Failed to start student-sync drainer: {e}", exc_info=True)

    # CRM audit outbox drainer: delivers LMS-owned audit events (group, membership, lesson
    # and attendance changes made inside the LMS) to the CRM. No-op until
    # CRM_AUDIT_INGEST_URL is set; scheduler-container only so the API never double-drains.
    try:
        import threading
        from src.crm_audit.drainer import run_drain_loop as run_crm_audit_drain

        threading.Thread(
            target=run_crm_audit_drain,
            kwargs={"poll_seconds": int(os.getenv("CRM_AUDIT_POLL_SECONDS", "20"))},
            daemon=True,
            name="crm-audit-drainer",
        ).start()
    except Exception as e:
        logger.error(f"Failed to start crm-audit drainer: {e}", exc_info=True)

    # Trial-access bookkeeping: flips expired trial grants' status (enforcement is
    # request-time; this only keeps admin list views truthful). Scheduler-container only.
    try:
        from src.services.trial_status_job import TrialStatusScheduler
        TrialStatusScheduler(check_interval=int(os.getenv("TRIAL_STATUS_POLL", "300"))).start()
    except Exception as e:
        logger.error(f"Failed to start trial status scheduler: {e}", exc_info=True)

    # Platform Integration Pack nightly job (03:30 Asia/Almaty): re-resolve unresolved platform
    # events, reconcile the last 7 days from IELTS, prune events older than 400 days. No-op
    # unless PLATFORM_EVENTS_INGEST_ENABLED; scheduler-container only.
    try:
        from src.integrations.reconcile import PlatformNightlyScheduler
        PlatformNightlyScheduler().start()
    except Exception as e:
        logger.error(f"Failed to start platform nightly scheduler: {e}", exc_info=True)

    # Check configuration
    resend_api_key = os.getenv('RESEND_API_KEY')
    postgres_url = os.getenv('POSTGRES_URL')

    if not resend_api_key:
        logger.error("❌ RESEND_API_KEY not configured!")
        logger.error("   Scheduler cannot send emails without API key")
        sys.exit(1)

    if not postgres_url:
        logger.error("❌ POSTGRES_URL not configured!")
        logger.error("   Scheduler cannot access database")
        sys.exit(1)

    logger.info(f"✅ Configuration validated")
    logger.info(f"   RESEND_API_KEY: {'*' * 10}{resend_api_key[-6:]}")
    logger.info(f"   POSTGRES_URL: {postgres_url.split('@')[0].split(':')[0]}://***")
    logger.info(f"   EMAIL_SENDER: {os.getenv('EMAIL_SENDER', 'noreply@mail.mastereducation.kz')}")
    logger.info(f"   EMAIL_SENDER_NAME: {os.getenv('EMAIL_SENDER_NAME', 'MasterED Platform')}")

    # Initialize scheduler
    logger.info("")
    logger.info("🔧 Initializing scheduler...")
    scheduler = LessonReminderScheduler(check_interval=60)  # Check every minute

    # Start scheduler
    scheduler.start()
    logger.info("✅ Scheduler started successfully!")
    logger.info("")
    logger.info("📋 Scheduler Configuration:")
    logger.info(f"   Check interval: 60 seconds")
    logger.info(f"   Reminder window: 28-32 minutes before lesson")
    logger.info(f"   Timezone: UTC (converts to Kazakhstan time in emails)")
    logger.info("")
    logger.info("🔄 Scheduler is now running... (Press Ctrl+C to stop)")
    logger.info("=" * 80)

    try:
        # Keep the process running
        while True:
            time.sleep(60)
            logger.debug("🔄 Scheduler heartbeat...")
    except KeyboardInterrupt:
        logger.info("")
        logger.info("⏹️  Received stop signal")
        scheduler.stop()
        if room_closer_worker:
            room_closer_worker.stop()
        if video_worker:
            video_worker.stop()
        for worker in (ingest_worker, recordings_worker):
            if worker:
                worker.stop()
        # Whatever a recording or transcript was cut off in the middle of gets its attempt back, and no
        # page is left showing a recording as processing — before the container goes.
        from src.services import recordings_status, work_in_flight
        work_in_flight.give_back_attempts()
        if ingest_worker and ingest_worker.running:
            recordings_status.ingest_finished()
        logger.info("✅ Scheduler stopped gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Scheduler crashed: {e}", exc_info=True)
        scheduler.stop()
        if room_closer_worker:
            room_closer_worker.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
