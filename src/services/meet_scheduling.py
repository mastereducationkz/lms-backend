"""Give a lesson a Google Meet link, by creating a Calendar event the robot owns.

The robot (``recordings@``) is the **organiser**; the teacher is an invited guest. That
is not a stylistic choice — a Meet recording lands in the Drive of whoever organised the
calendar event, so making the robot the organiser is what keeps every recording in one
Drive we control, instead of scattered across teachers' personal Drives where they
vanish when someone leaves (spec §4.3).

**Students are deliberately not invited.** Lessons average ~10 students, so inviting them
would put every student's personal email on an invite visible to their classmates, send
~20k invitations across the schedule, and re-email everyone on every reschedule — and
this LMS reschedules a lot. Instead the meeting is created with open access so anyone
holding the link joins without knocking, and the link reaches students through the LMS.
Owner decision, 2026-09-10.
"""
import logging
import uuid
from typing import Optional

from src.services import google_workspace

logger = logging.getLogger(__name__)

# The robot's own calendar. Events are created here, not on the teacher's calendar, so
# that organiser-ship (and therefore recording ownership) stays with the robot.
ORGANISER_CALENDAR = "primary"


class MeetSchedulingError(RuntimeError):
    """Calendar/Meet refused to create the conference for this lesson."""


def _request_id(event_id: int) -> str:
    """Deterministic conference request id.

    Google treats ``conferenceData.createRequest.requestId`` as an idempotency key: the
    same id returns the existing conference instead of minting a second one. Deriving it
    from the lesson id means a retry after a timeout re-attaches to the conference we
    already made, rather than leaving an orphan Meet nobody will ever join.
    """
    return f"lms-lesson-{event_id}"


def _teacher_workspace_email(event) -> Optional[str]:
    teacher = getattr(event, "teacher", None)
    return getattr(teacher, "workspace_email", None) if teacher else None


def ensure_meet_link(db, event) -> Optional[str]:
    """Return the lesson's Meet link, creating the calendar event if needed.

    Idempotent by design, and cheap to call on every scheduler tick:

    * already has ``meeting_url`` → returns it, makes no API call at all;
    * teacher has no ``workspace_email`` → returns None. This is the rollout switch:
      only teachers who have been onboarded get links, so widening the pilot is a data
      change rather than a deploy.
    """
    if event.meeting_url:
        return event.meeting_url

    organiser_email = _teacher_workspace_email(event)
    if not organiser_email:
        logger.debug("lesson %s: teacher has no workspace_email — skipping", event.id)
        return None

    body = {
        "summary": event.title,
        "description": (event.description or "")[:8000],
        "start": {"dateTime": event.start_datetime.isoformat(), "timeZone": "Asia/Almaty"},
        "end": {"dateTime": event.end_datetime.isoformat(), "timeZone": "Asia/Almaty"},
        # Only the teacher. See the module docstring for why students are excluded.
        "attendees": [{"email": organiser_email}],
        # Belt and braces: even with a one-person guest list, never leak the list itself.
        "guestsCanSeeOtherGuests": False,
        "guestsCanInviteOthers": False,
        "conferenceData": {
            "createRequest": {
                "requestId": _request_id(event.id),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    service = google_workspace.calendar_client()
    try:
        created = service.events().insert(
            calendarId=ORGANISER_CALENDAR,
            body=body,
            # Without conferenceDataVersion=1 Google silently drops conferenceData and
            # returns a perfectly valid event with no Meet link — the classic failure
            # here, and silent, so it is pinned by a test.
            conferenceDataVersion=1,
            # The teacher is told about the new workflow out of band; a burst of invite
            # emails for lessons they already know about is noise. Reschedules later can
            # still notify if we choose.
            sendUpdates="none",
        ).execute()
    except Exception as e:  # googleapiclient raises HttpError, but also socket errors
        raise MeetSchedulingError(f"lesson {event.id}: {e}") from e

    link = created.get("hangoutLink")
    if not link:
        raise MeetSchedulingError(
            f"lesson {event.id}: calendar event {created.get('id')} created without a "
            "Meet link — conferenceDataVersion or the conference solution was rejected"
        )

    event.meeting_url = link
    db.commit()
    logger.info("lesson %s: Meet link created (calendar event %s)", event.id, created.get("id"))
    return link
