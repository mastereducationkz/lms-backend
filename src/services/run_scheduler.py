#!/usr/bin/env python3
"""
Standalone Lesson Reminder Scheduler Runner
Runs the scheduler in a separate process/container
"""
import logging
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

def main():
    """Main scheduler runner"""
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
        if video_worker:
            video_worker.stop()
        logger.info("✅ Scheduler stopped gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Scheduler crashed: {e}", exc_info=True)
        scheduler.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()
