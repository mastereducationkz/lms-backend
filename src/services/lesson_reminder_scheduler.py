"""
Lesson Reminder Scheduler
Periodically checks for upcoming lessons (Events) and sends email reminders 30 minutes before.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from sqlalchemy.orm import Session

from src.config import SessionLocal
from src.schemas.models import Event, EventGroup, EventParticipant, UserInDB, Group, GroupStudent
from src.services import email_log
from src.services.email_service import (
    reminder_idempotency_key,
    send_lesson_reminder_notification,
)
from src.utils.push_notifications import send_push_notification

logger = logging.getLogger(__name__)


class LessonReminderScheduler:
    """Background scheduler to send reminders for upcoming class events"""
    
    def __init__(self, check_interval: int = 60):
        """
        Initialize the scheduler
        
        Args:
            check_interval: How often to check for upcoming events (in seconds)
        """
        self.check_interval = check_interval
        self.running = False
        self.thread = None
        # Fast path only. This set is what stops the scheduler re-querying an event it
        # already handled during the 4-minute reminder window; it is *not* what stops
        # duplicate mail, because it dies with the process and this scheduler runs in its
        # own container that restarts on every deploy. The durable guarantee is the
        # unique idempotency_key claimed in email_log before each send.
        self.sent_reminders = set()

    def start(self):
        """Start the scheduler in a background thread"""
        if self.running:
            logger.warning("Lesson reminder scheduler is already running")
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("✅ Lesson reminder scheduler started")
        
    def stop(self):
        """Stop the scheduler"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("🛑 Lesson reminder scheduler stopped")
        
    def _run(self):
        """Main scheduler loop"""
        logger.info("� [SCHEDULER] Lesson reminder scheduler thread started")
        logger.info(f"   Check interval: {self.check_interval} seconds")
        logger.info(f"   Pre-lesson reminder window: 28-32 minutes before lesson")
        logger.info(f"   Post-lesson reminder window: 15-20 minutes after lesson")
        
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                logger.info(f"⏰ [SCHEDULER] Checking at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
                self._check_and_send_reminders()
                self._check_and_send_post_lesson_reminders()
            except Exception as e:
                logger.error(f"❌ [SCHEDULER] Error in lesson reminder scheduler: {e}", exc_info=True)
            
            # Wait for next check
            time.sleep(self.check_interval)
    
    def _check_and_send_reminders(self):
        """Check for upcoming lesson events and send reminders"""
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Look for lessons starting in 28-32 minutes (to account for check interval)
            reminder_time_start = now + timedelta(minutes=28)
            reminder_time_end = now + timedelta(minutes=32)
            
            logger.debug(f"🔍 [SCHEDULER] Checking for events between {reminder_time_start.strftime('%H:%M')} and {reminder_time_end.strftime('%H:%M')} UTC")
            
            # Query for active class events in the reminder window
            upcoming_events = db.query(Event).filter(
                Event.is_active == True,
                Event.event_type == 'class',  # Only class events (lessons)
                Event.start_datetime >= reminder_time_start,
                Event.start_datetime <= reminder_time_end
            ).all()
            
            if not upcoming_events:
                logger.debug(f"✓ [SCHEDULER] No upcoming class events found in the next 30 minutes")
                return
                
            logger.info(f"📅 [SCHEDULER] Found {len(upcoming_events)} upcoming class event(s) for reminders")
            
            for event in upcoming_events:
                # Create unique key to avoid duplicate sends
                reminder_key = f"event_{event.id}_{event.start_datetime.isoformat()}"
                
                if reminder_key in self.sent_reminders:
                    logger.debug(f"⏭️  [SCHEDULER] Already sent reminder for event ID {event.id}, skipping")
                    continue  # Already sent reminder for this event
                
                logger.info(f"📨 [SCHEDULER] Processing reminder for event ID {event.id} at {event.start_datetime.strftime('%H:%M')}")
                
                # Send reminders for this event
                success = self._send_event_reminders(db, event)
                
                if success:
                    self.sent_reminders.add(reminder_key)
                    logger.info(f"✅ [SCHEDULER] Marked event ID {event.id} as sent")
                    
                    # Clean up old entries from sent_reminders to prevent memory bloat
                    # Keep only reminders from last 24 hours
                    cutoff_time = now - timedelta(hours=24)
                    old_count = len(self.sent_reminders)
                    self.sent_reminders = {
                        key for key in self.sent_reminders 
                        if '_' in key and len(key.split('_')) >= 3 and
                        datetime.fromisoformat(key.split('_', 2)[2]) > cutoff_time
                    }
                    cleaned = old_count - len(self.sent_reminders)
                    if cleaned > 0:
                        logger.debug(f"🧹 [SCHEDULER] Cleaned {cleaned} old reminder(s) from cache")
                else:
                    logger.error(f"❌ [SCHEDULER] Failed to send reminders for event ID {event.id}")
                    
        except Exception as e:
            logger.error(f"❌ [SCHEDULER] Error checking for lesson reminders: {e}", exc_info=True)
        finally:
            db.close()
    
    def _check_and_send_post_lesson_reminders(self):
        """Check for recently finished lessons and send reminders to teachers if attendance missing"""
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            # Look for lessons that ended 15-20 minutes ago
            # If check_interval is 60s, a 5-minute window is safe
            reminder_time_start = now - timedelta(minutes=20)
            reminder_time_end = now - timedelta(minutes=15)
            
            # Query for active class events that ended in this window
            ended_events = db.query(Event).filter(
                Event.is_active == True,
                Event.event_type == 'class',
                Event.end_datetime >= reminder_time_start,
                Event.end_datetime <= reminder_time_end
            ).all()
            
            if not ended_events:
                return

            logger.info(f"📅 [SCHEDULER] Found {len(ended_events)} recently ended class event(s)")
            
            for event in ended_events:
                # Unique key for post-lesson reminder
                reminder_key = f"post_lesson_{event.id}_{event.end_datetime.isoformat()}"
                
                if reminder_key in self.sent_reminders:
                    continue
                
                # Check if attendance is recorded
                # We check if there are any participants registered for this event
                attendance_count = db.query(EventParticipant).filter(
                    EventParticipant.event_id == event.id
                ).count()
                
                # If attendance is empty or very low, send reminder
                # We can't know exact expected count easily without querying groups, 
                # but 0 means definitely no attendance
                if attendance_count == 0:
                    logger.info(f"⚠️  [POST-LESSON] Missing attendance for event {event.id}. sending notification.")
                    success = self._send_post_lesson_notification(db, event)
                    if success:
                        self.sent_reminders.add(reminder_key)
                        logger.info(f"✅ [POST-LESSON] Notification sent for event {event.id}")
                else:
                    logger.info(f"✓ [POST-LESSON] Attendance already recorded for event {event.id} ({attendance_count} records)")
                    # Mark as 'sent' so we don't check again
                    self.sent_reminders.add(reminder_key)
                    
        except Exception as e:
            logger.error(f"❌ [SCHEDULER] Error checking post-lesson reminders: {e}", exc_info=True)
        finally:
            db.close()
    
    def _send_post_lesson_notification(self, db: Session, event: Event) -> bool:
        """Send push notification to teacher about missing attendance/scores"""
        try:
            # Find teachers for this event
            event_groups = db.query(EventGroup).filter(EventGroup.event_id == event.id).all()
            if not event_groups:
                return False
                
            teacher_ids = set()
            for eg in event_groups:
                group = db.query(Group).filter(Group.id == eg.group_id).first()
                if group and group.teacher_id:
                    teacher_ids.add(group.teacher_id)
            
            if not teacher_ids:
                return False
                
            teachers = db.query(UserInDB).filter(
                UserInDB.id.in_(teacher_ids),
                UserInDB.is_active == True,
                UserInDB.push_token.isnot(None) # Only if they have push token
            ).all()
            
            sent_count = 0
            for teacher in teachers:
                if not teacher.push_token:
                    continue
                    
                title = "⏳ Missing Attendance & Scores"
                body = f"Lesson '{event.title}' has ended. Please record attendance and activity scores."
                
                success = send_push_notification(
                    push_token=teacher.push_token,
                    title=title,
                    body=body,
                    data={"type": "missing_attendance", "eventId": event.id}
                )
                if success:
                    sent_count += 1
            
            return sent_count > 0
            
        except Exception as e:
            logger.error(f"❌ [POST-LESSON] Error sending notification: {e}")
            return False
    
    def _send_event_reminders(self, db: Session, event: Event) -> bool:
        """
        Send reminders for a specific class event
        
        Args:
            db: Database session
            event: Event to send reminders for
            
        Returns:
            True if reminders sent successfully
        """
        try:
            logger.info(f"🎯 [REMINDER] Processing event ID {event.id}")
            logger.info(f"   📚 Event: '{event.title}' (Type: {event.event_type})")
            
            # Convert UTC to Kazakhstan time (GMT+5) for display in email
            KZ_OFFSET = timedelta(hours=5)
            event_datetime_kz = event.start_datetime + KZ_OFFSET
            event_datetime_str = event_datetime_kz.strftime("%d.%m.%Y в %H:%M")
            
            # Get groups associated with this event
            event_groups = db.query(EventGroup).filter(
                EventGroup.event_id == event.id
            ).all()
            
            if not event_groups:
                logger.warning(f"⚠️  [REMINDER] No groups found for event {event.id}")
                return False
            
            logger.info(f"   � Found {len(event_groups)} group(s) for this event")
            
            sent_count = 0
            failed_count = 0
            skipped_count = 0

            def _record(result, event_id: int, user_id: int, who: str) -> None:
                """Classify one send. A ``None`` result is ambiguous — a genuine failure
                and an already-claimed reminder look identical to the caller — so ask the
                journal which it was. Only reached on the rare non-success path."""
                nonlocal sent_count, failed_count, skipped_count
                if result:
                    sent_count += 1
                    return
                key = reminder_idempotency_key(event_id, user_id)
                if key and email_log.claimed_elsewhere(key):
                    skipped_count += 1
                    logger.debug("⏭️  [REMINDER] %s already reminded for event %s", who, event_id)
                else:
                    failed_count += 1

            # Collect unique teachers from groups (teacher_id)
            teacher_ids = set()
            for event_group in event_groups:
                group = db.query(Group).filter(Group.id == event_group.group_id).first()
                if group and group.teacher_id:
                    teacher_ids.add(group.teacher_id)
            
            # Get teacher objects
            teachers = []
            if teacher_ids:
                teachers = db.query(UserInDB).filter(
                    UserInDB.id.in_(teacher_ids),
                    UserInDB.is_active == True,
                    UserInDB.email.isnot(None)
                ).all()
            
            logger.info(f"   👨‍🏫 Found {len(teachers)} unique teacher(s) from groups")
            
            # Process each group to get students
            for event_group in event_groups:
                group = db.query(Group).filter(Group.id == event_group.group_id).first()
                if not group:
                    logger.warning(f"⚠️  [REMINDER] Group {event_group.group_id} not found")
                    continue
                
                logger.info(f"   📋 Processing group: '{group.name}' (ID: {group.id})")
                
                # Get students from the group
                students = db.query(UserInDB).join(
                    GroupStudent, UserInDB.id == GroupStudent.student_id
                ).filter(
                    GroupStudent.group_id == group.id,
                    UserInDB.is_active == True,
                    UserInDB.email.isnot(None)
                ).all()
                
                logger.info(f"      👨‍🎓 Found {len(students)} active student(s) with email in group")
                
                # Send reminders to all students in this group
                logger.info(f"      📤 Sending reminders to students...")
                for student in students:
                    try:
                        result = send_lesson_reminder_notification(
                            to_email=student.email,
                            recipient_name=student.name or student.email.split('@')[0],
                            lesson_title=event.title,
                            lesson_datetime=event_datetime_str,
                            group_name=group.name,
                            role="student",
                            event_id=event.id,
                            user_id=student.id,
                        )
                        _record(result, event.id, student.id, f"student {student.id}")
                    except Exception as e:
                        logger.error(f"❌ [REMINDER] Failed to send reminder to student {student.email}: {e}")
                        failed_count += 1
            
            # Send reminders to teachers (from Group.teacher_id)
            if teachers:
                logger.info(f"   📤 Sending reminders to {len(teachers)} teacher(s)...")
                for teacher in teachers:
                    try:
                        # Find all groups where this teacher teaches that are part of this event
                        teacher_groups = [
                            db.query(Group).filter(Group.id == eg.group_id).first()
                            for eg in event_groups
                            if db.query(Group).filter(
                                Group.id == eg.group_id,
                                Group.teacher_id == teacher.id
                            ).first()
                        ]
                        
                        # Use first group name or "Multiple Groups"
                        group_names = [g.name for g in teacher_groups if g]
                        group_name = group_names[0] if group_names else "Multiple Groups"
                        
                        result = send_lesson_reminder_notification(
                            to_email=teacher.email,
                            recipient_name=teacher.name or teacher.email.split('@')[0],
                            lesson_title=event.title,
                            lesson_datetime=event_datetime_str,
                            group_name=group_name,
                            role="teacher",
                            event_id=event.id,
                            user_id=teacher.id,
                        )
                        if result:
                            logger.info(f"      ✅ Sent to teacher: {teacher.email} (Group: {group_name})")
                        _record(result, event.id, teacher.id, f"teacher {teacher.id}")
                    except Exception as e:
                        logger.error(f"❌ [REMINDER] Failed to send reminder to teacher {teacher.email}: {e}")
                        failed_count += 1
            else:
                logger.warning(f"⚠️  [REMINDER] No teachers found for event {event.id}")
            
            logger.info(
                f"✅ [REMINDER] Completed for event '{event.title}' "
                f"(Time: {event_datetime_str})"
            )
            logger.info(
                f"   📊 Sent: {sent_count}, Failed: {failed_count}, Skipped: {skipped_count}"
            )

            # True means "this event has been dealt with", not "every email landed". A
            # failed send is already claimed in the journal and will never be retried, so
            # re-processing the event on the next tick could only re-query the database to
            # reach the same conclusion.
            return (sent_count + failed_count + skipped_count) > 0
            
        except Exception as e:
            logger.error(f"❌ [REMINDER] Error sending event reminders for event {event.id}: {e}", exc_info=True)
            return False


# Global scheduler instance
_scheduler: Optional[LessonReminderScheduler] = None


def get_scheduler() -> LessonReminderScheduler:
    """Get or create the global scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = LessonReminderScheduler(check_interval=60)  # Check every minute
    return _scheduler


def start_lesson_reminder_scheduler():
    """Start the lesson reminder scheduler"""
    scheduler = get_scheduler()
    scheduler.start()


def stop_lesson_reminder_scheduler():
    """Stop the lesson reminder scheduler"""
    scheduler = get_scheduler()
    scheduler.stop()
