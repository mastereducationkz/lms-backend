"""Give a lesson a Google Meet link, by creating a Calendar event the robot owns.

The robot (``recordings@``) is the **organiser**; the teacher is an invited guest. That
is not a stylistic choice — a Meet recording lands in the Drive of whoever organised the
calendar event, so making the robot the organiser is what keeps every recording in one
Drive we control, instead of scattered across teachers' personal Drives where they
vanish when someone leaves (spec §4.3).

**Students are deliberately not invited.** Lessons average ~10 students, so inviting them
would put every student's personal email on an invite visible to their classmates, send
~20k invitations across the schedule, and re-email everyone on every reschedule — and
this LMS reschedules a lot. Instead the meeting is opened to anyone holding the link, and
the link reaches students through the LMS. Owner decision, 2026-09-10.

**The Meet space is created through the Meet API, not by Calendar.** This is not a detail.
A space that Calendar creates as a side effect of an event is not "created by" this app,
so with the ``meetings.space.created`` scope we get **403 on every read of it** — verified.
That would have broken the pipeline in two silent ways: students would have had to knock
to get in (a Calendar-made space defaults to ``accessType: TRUSTED``), and the poller
could never have matched a conference back to its lesson, because matching reads the
space. Creating the space ourselves fixes both: we can set it OPEN, and we can read it
later.
"""
import logging
from typing import Optional

from src.services import google_workspace

logger = logging.getLogger(__name__)

# The robot's own calendar. Events are created here, not on the teacher's calendar, so
# that organiser-ship (and therefore recording ownership) stays with the robot.
ORGANISER_CALENDAR = "primary"


class MeetSchedulingError(RuntimeError):
    """Calendar/Meet refused to create the conference for this lesson."""


def _teacher_workspace_email(event) -> Optional[str]:
    teacher = getattr(event, "teacher", None)
    return getattr(teacher, "workspace_email", None) if teacher else None


def create_open_space() -> tuple:
    """Create a Meet space that anyone with the link may join. Returns (uri, name).

    Two API calls, and both matter:

    ``spaces.create`` makes the space *ours*, which is what later lets us read it back —
    a space Calendar creates returns 403 under the ``meetings.space.created`` scope.

    ``spaces.patch`` to ``accessType=OPEN`` is what lets students in. A new space defaults
    to ``TRUSTED``, under which an anonymous joiner has to knock and be admitted — roughly
    ten knocks per lesson, every lesson. OPEN is a deliberate trade: anyone holding the
    link can join, including someone it was forwarded to, which is the same exposure the
    link itself already carries.

    Moderation is left OFF (the default), so there are no host controls to lock the
    teacher out of presenting or recording — the robot is not in the room to grant them.
    """
    meet = google_workspace.meet_client()
    space = meet.spaces().create(body={}).execute()
    name = space["name"]
    meet.spaces().patch(
        name=name,
        updateMask="config.accessType",
        body={"config": {"accessType": "OPEN"}},
    ).execute()
    return space["meetingUri"], name


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

    try:
        space_uri, space_name = create_open_space()
    except Exception as e:
        raise MeetSchedulingError(f"lesson {event.id}: could not create Meet space: {e}") from e

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
        # Attach the space we already made, rather than asking Calendar to mint one.
        "conferenceData": {
            "conferenceId": space_uri.rsplit("/", 1)[-1],
            "conferenceSolution": {
                "key": {"type": "hangoutsMeet"},
                "name": "Google Meet",
            },
            "entryPoints": [{"entryPointType": "video", "uri": space_uri}],
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

    # Trust our own space URI over hangoutLink: the event carries the conference we
    # attached, and hangoutLink is only populated for Calendar-created conferences.
    link = created.get("hangoutLink") or space_uri
    logger.debug("lesson %s attached space %s", event.id, space_name)

    event.meeting_url = link
    db.commit()
    logger.info("lesson %s: Meet link created (calendar event %s)", event.id, created.get("id"))
    return link
