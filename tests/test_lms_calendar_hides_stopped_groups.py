"""Every LMS surface that looks ahead asks the calendar's question, and gets the calendar's answer.

On 2026-09-10 the LMS drew 232 upcoming lessons the CRM hid: 84 in groups that had been
switched off, 148 in groups with nobody enrolled. They were not only drawn. Their rosters were
reminded to attend them (82 emails to 12 people in the preceding fortnight), and the recordings
worker gave seven of them Meet rooms and invites on a teacher's calendar.

The rule lives in ``src/services/operational_groups.py`` and is tested there. What is tested
here is that each surface actually asks it — the endpoints a student, teacher and parent read,
the reminder scheduler, the Meet-link worker and the missing-recording sweep — and that none of
them lets go of the past.
"""
from __future__ import annotations

import pytest

from tests.test_operational_groups import db, world  # noqa: F401 - transactional fixtures


@pytest.fixture
def two_groups(world):
    """A running group and one switched off with its student still enrolled (group 223)."""
    live = world["group"](name="live")
    world["enrol"](live)
    stopped = world["group"](name="Indi Aldiyar SAT 2026 - Gulzada", is_active=False)
    world["enrol"](stopped)
    return live, stopped


# --- what people read --------------------------------------------------------------------------


def _calendar(world, lesson):
    """The teacher's calendar for the month the lesson falls in (cache bypassed)."""
    from src.events.routes.events import get_calendar_events

    when = lesson.start_datetime
    return {
        e.id for e in get_calendar_events.__wrapped__(
            year=when.year, month=when.month, db=world["db"], current_user=world["teacher"],
        )
    }


def test_the_calendar_drops_a_stopped_groups_next_lesson_and_keeps_its_past(world, two_groups):
    live, stopped = two_groups
    ahead = world["lesson"](live, days_ahead=2)
    phantom = world["lesson"](stopped, days_ahead=2)
    taught = world["lesson"](stopped, days_ahead=-3)

    assert ahead.id in _calendar(world, ahead)
    assert phantom.id not in _calendar(world, phantom)
    assert taught.id in _calendar(world, taught), "a lesson that happened stays on the calendar"


def test_include_finished_false_drops_a_stopped_groups_past_lesson_too(world, two_groups):
    """The calendar's own opt-out (2026-09-12): recordings have their own calendar now, so
    the default HTTP behaviour — ``include_finished=False`` — no longer keeps the archive."""
    from src.events.routes.events import get_calendar_events

    _, stopped = two_groups
    taught = world["lesson"](stopped, days_ahead=-3)

    when = taught.start_datetime
    shown = {
        e.id for e in get_calendar_events.__wrapped__(
            year=when.year, month=when.month, include_finished=False,
            db=world["db"], current_user=world["teacher"],
        )
    }
    assert taught.id not in shown


def test_include_finished_true_restores_it(world, two_groups):
    from src.events.routes.events import get_calendar_events

    _, stopped = two_groups
    taught = world["lesson"](stopped, days_ahead=-3)

    when = taught.start_datetime
    shown = {
        e.id for e in get_calendar_events.__wrapped__(
            year=when.year, month=when.month, include_finished=True,
            db=world["db"], current_user=world["teacher"],
        )
    }
    assert taught.id in shown


def test_the_dashboards_upcoming_list_drops_it_too(world, two_groups):
    from src.events.routes.events import get_my_events

    live, stopped = two_groups
    ahead = world["lesson"](live)
    phantom = world["lesson"](stopped)

    listed = {
        e.id for e in get_my_events(
            skip=0, limit=100, event_type=None, group_id=None, start_date=None,
            end_date=None, upcoming_only=True, db=world["db"], current_user=world["teacher"],
        )
    }

    assert ahead.id in listed and phantom.id not in listed


def test_the_groups_own_page_still_lists_everything(world, two_groups):
    """Asked for one group by name is the group's own page, which lists what it holds."""
    from src.events.routes.events import get_my_events

    _, stopped = two_groups
    phantom = world["lesson"](stopped)

    listed = {
        e.id for e in get_my_events(
            skip=0, limit=100, event_type=None, group_id=stopped.id, start_date=None,
            end_date=None, upcoming_only=True, db=world["db"], current_user=world["teacher"],
        )
    }

    assert phantom.id in listed


def test_a_parents_month_view_drops_it(world, two_groups):
    from src.parents.routes import child_attendance
    from src.schemas.models import GroupStudent, ParentStudent, UserInDB

    live, stopped = two_groups
    db = world["db"]
    child = db.query(UserInDB).join(GroupStudent, GroupStudent.student_id == UserInDB.id) \
        .filter(GroupStudent.group_id == stopped.id).one()
    db.add(GroupStudent(group_id=live.id, student_id=child.id)); db.flush()
    parent = UserInDB(email=f"parent-{child.id}@t.local", name="parent", role="parent",
                      hashed_password="x", is_active=True)
    db.add(parent); db.flush()
    db.add(ParentStudent(parent_id=parent.id, student_id=child.id)); db.flush()
    ahead = world["lesson"](live)
    phantom = world["lesson"](stopped)

    when = ahead.start_datetime
    shown = {row["event_id"] for row in child_attendance(
        child.id, year=when.year, month=when.month, current_user=parent, db=db)}

    assert ahead.id in shown and phantom.id not in shown


# --- what the system does on its own -------------------------------------------------------------


class _Borrowed:
    """Hands the scheduler the test's session; its ``close()`` must not end the test."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def _run_scheduler_step(world, monkeypatch, step, sender):
    from sqlalchemy import text

    from src.services import lesson_reminder_scheduler as lrs

    # The scheduler compares naive UTC columns with an aware `now`; Postgres resolves that in
    # the session time zone, which on a developer machine is not UTC.
    world["db"].execute(text("SET LOCAL TIME ZONE 'UTC'"))
    monkeypatch.setattr(lrs, "SessionLocal", lambda: _Borrowed(world["db"]))
    touched = []

    def _record(self, db, event):
        touched.append(event.id)
        return False  # keeps the in-memory de-dup cache out of the picture

    monkeypatch.setattr(lrs.LessonReminderScheduler, sender, _record)
    getattr(lrs.LessonReminderScheduler(), step)()
    return touched


def test_nobody_is_reminded_of_a_lesson_the_calendar_hides(world, two_groups, monkeypatch):
    live, stopped = two_groups
    soon = world["lesson"](live, days_ahead=30 / 1440)
    phantom = world["lesson"](stopped, days_ahead=30 / 1440)

    reminded = _run_scheduler_step(world, monkeypatch, "_check_and_send_reminders",
                                   "_send_event_reminders")

    assert soon.id in reminded and phantom.id not in reminded


def test_nobody_is_asked_for_a_register_of_a_lesson_nobody_taught(world, two_groups, monkeypatch):
    live, stopped = two_groups
    # Ended 17 minutes ago: inside the post-lesson window (15–20 minutes after the end).
    taught = world["lesson"](live, days_ahead=-77 / 1440)
    phantom = world["lesson"](stopped, days_ahead=-77 / 1440)

    asked = _run_scheduler_step(world, monkeypatch, "_check_and_send_post_lesson_reminders",
                                "_send_post_lesson_notification")

    assert taught.id in asked and phantom.id not in asked


def test_meet_rooms_are_made_only_for_lessons_the_calendar_shows(world, two_groups, monkeypatch):
    """Also the shape that joins the *teacher's* `users` row — the clause's own `users` (the
    roster check) must stay its own, or every lesson would look empty."""
    from src.services import meet_scheduling, recordings_worker

    live, stopped = two_groups
    world["teacher"].workspace_email = "teacher@mastereducation.kz"
    world["db"].flush()
    kept = world["lesson"](live)
    phantom = world["lesson"](stopped)
    asked = []
    monkeypatch.setattr(meet_scheduling, "ensure_meet_link",
                        lambda _db, lesson: asked.append(lesson.id) or False)

    recordings_worker.ensure_upcoming_meet_links(world["db"])

    assert kept.id in asked and phantom.id not in asked


def test_a_stopped_groups_silent_lesson_is_not_a_missing_recording(world, two_groups):
    from src.schemas.models import MissingRecordingLog
    from src.services.recording_alerts import sweep_missing_recordings

    live, stopped = two_groups
    world["teacher"].workspace_email = "teacher@mastereducation.kz"
    world["db"].flush()
    taught = world["lesson"](live, days_ahead=-0.5, meeting_url="https://meet.google.com/aaa-bbbb-ccc")
    phantom = world["lesson"](stopped, days_ahead=-0.5, meeting_url="https://meet.google.com/ddd-eeee-fff")

    sweep_missing_recordings(world["db"])

    flagged = {row.event_id for row in world["db"].query(MissingRecordingLog).all()}
    assert taught.id in flagged and phantom.id not in flagged
