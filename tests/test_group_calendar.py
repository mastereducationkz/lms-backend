"""The per-group Google Calendar: which groups get one, and that it holds exactly their entries.

Google is a fake that keeps calendars and events in dictionaries; what is asserted is the
calls the sync makes and the state it leaves — including that a calendar never carries a raw
Meet link and that a missing scope or a spent quota never becomes a crash.
"""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.announcements.models import TelegramGroupLink
from src.assignments.models import Assignment
from src.events.calendar_models import GroupGoogleCalendar
from src.schemas.models import AppSetting
from src.services import calendar_items, group_calendar
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


class FakeHttpError(Exception):
    def __init__(self, status, content):
        super().__init__(content)
        self.resp = SimpleNamespace(status=status)
        self.content = content.encode()


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGoogle:
    def __init__(self):
        self.calendars_store, self.events_store, self.calls = {}, {}, []
        self.fail_insert_calendar = None
        outer = self

        class Calendars:
            def insert(self, body):
                def run():
                    outer.calls.append(("calendars.insert", body["summary"]))
                    if outer.fail_insert_calendar:
                        raise outer.fail_insert_calendar
                    cid = f"cal{len(outer.calendars_store) + 1}@group.calendar.google.com"
                    outer.calendars_store[cid] = dict(body)
                    outer.events_store[cid] = {}
                    return {"id": cid}
                return _Req(run)

            def patch(self, calendarId, body):
                return _Req(lambda: outer.calls.append(("calendars.patch", calendarId)) or
                            outer.calendars_store[calendarId].update(body))

        class Acl:
            def insert(self, calendarId, body):
                return _Req(lambda: outer.calls.append(("acl.insert", calendarId, body["scope"]["type"])))

        class Events:
            def list(self, calendarId, **kwargs):
                return _Req(lambda: {"items": list(outer.events_store[calendarId].values())})

            def insert(self, calendarId, body):
                def run():
                    outer.calls.append(("events.insert", body["id"]))
                    outer.events_store[calendarId][body["id"]] = body
                return _Req(run)

            def update(self, calendarId, eventId, body):
                def run():
                    outer.calls.append(("events.update", eventId))
                    outer.events_store[calendarId][eventId] = {**body, "id": eventId}
                return _Req(run)

            def delete(self, calendarId, eventId):
                def run():
                    outer.calls.append(("events.delete", eventId))
                    outer.events_store[calendarId].pop(eventId, None)
                return _Req(run)

        self._calendars, self._acl, self._events = Calendars(), Acl(), Events()

    def calendars(self):
        return self._calendars

    def acl(self):
        return self._acl

    def events(self):
        return self._events

    def names(self, prefix):
        return [call for call in self.calls if call[0] == prefix]


NOW = datetime(2026, 9, 14, 6, 0)


@pytest.fixture(autouse=True)
def _reset_pauses(monkeypatch):
    monkeypatch.setattr(group_calendar, "_paused_until", None)
    monkeypatch.setattr(group_calendar, "_creation_paused_until", None)


@pytest.fixture
def live(world):
    db = world["db"]
    world["teacher"].workspace_email = "said@mastereducation.kz"
    db.add(AppSetting(key="workspace_directory", value={"accounts": [
        {"email": "said@mastereducation.kz", "suspended": False},
        {"email": "ali@mastereducation.kz", "suspended": True}]}))
    counter = {"support": 500}

    def make(name="IELTS July 8 2026 - Саид", **flags):
        group = world["group"](name=name, **flags)
        world["enrol"](group)               # a group's lessons count only while it has students
        counter["support"] += 1
        db.add(TelegramGroupLink(lms_group_id=group.id, support_group_id=counter["support"], chat_title=name))
        db.flush()
        return group

    def lesson(group, day, hour=12, **fields):
        start = datetime(2026, 9, day, hour, 0)
        return world["lesson"](group, start_datetime=start, end_datetime=start + timedelta(hours=1), **fields)

    world.update(make=make, at=lesson)
    return world


# ── which groups get a calendar ──────────────────────────────────────────────────────────

def test_a_connected_linked_group_with_lessons_ahead_is_live(live):
    group = live["make"]()
    live["at"](group, 15)
    assert group_calendar.is_live(live["db"], group, NOW) is True


def test_what_makes_a_group_not_live(live):
    db = live["db"]
    no_lessons = live["make"](name="No lessons")
    assert group_calendar.is_live(db, no_lessons, NOW) is False

    unlinked = live["group"](name="Unlinked")
    live["enrol"](unlinked)
    live["at"](unlinked, 15)
    assert group_calendar.is_live(db, unlinked, NOW) is False

    over = live["make"](name="Over", is_over=True)
    live["at"](over, 15)
    assert group_calendar.is_live(db, over, NOW) is False


def test_a_suspended_or_unlisted_teacher_account_is_not_live(live):
    db = live["db"]
    group = live["make"]()
    live["at"](group, 15)
    live["teacher"].workspace_email = "ali@mastereducation.kz"          # suspended in the directory
    db.flush()
    assert group_calendar.is_live(db, group, NOW) is False
    live["teacher"].workspace_email = "nobody@mastereducation.kz"       # not in the directory
    db.flush()
    assert group_calendar.is_live(db, group, NOW) is False


def test_a_group_connected_only_through_a_substitute_is_not_live(live):
    db = live["db"]
    group = live["make"](name="Substitution only")
    group.teacher_id = _user(db, "teacher").id                          # regular teacher unconnected
    db.flush()
    live["at"](group, 15)                                               # taught by the connected teacher
    assert group_calendar.is_live(db, group, NOW) is False


# ── the calendar ─────────────────────────────────────────────────────────────────────────

def test_event_ids_are_valid_and_stable():
    first = group_calendar.google_event_id("lesson-18844")
    assert re.fullmatch(r"[a-v0-9]{5,1024}", first)
    assert first == group_calendar.google_event_id("lesson-18844")
    assert first != group_calendar.google_event_id("lesson-18845")
    assert re.fullmatch(r"[a-v0-9]+", group_calendar.google_event_id("weekly-7"))


def test_a_calendar_is_created_and_shared_once(live):
    db, google = live["db"], FakeGoogle()
    group = live["make"]()
    row = group_calendar.ensure_calendar(db, group, google)
    again = group_calendar.ensure_calendar(db, group, google)
    assert row.id == again.id and row.public is True
    assert len(google.names("calendars.insert")) == 1
    assert google.names("acl.insert") == [("acl.insert", row.calendar_id, "default")]


def test_sync_writes_exactly_the_groups_entries_and_follows_changes(live):
    db, google = live["db"], FakeGoogle()
    group = live["make"]()
    first = live["at"](group, 15, meeting_url="https://meet.google.com/abc-defg-hij", topic="Reading")
    second = live["at"](group, 17)
    live["at"](group, 16, event_type="weekly_test", title="IELTS Weekly Test",
               meeting_url="https://ielts.mastereducation.kz/weekly-sets/15")
    db.add(Assignment(group_id=group.id, title="Essay", assignment_type="homework", content="—",
                      is_active=True, is_hidden=False, due_date=datetime(2026, 9, 16, 20, 0)))
    db.flush()
    row = group_calendar.ensure_calendar(db, group, google)

    assert group_calendar.sync(db, group, row, google, NOW) == "synced"
    stored = google.events_store[row.calendar_id]
    assert len(stored) == 4
    blob = repr(stored)
    assert "meet.google.com" not in blob, "a shared calendar never hands out a Meet room"
    assert "/calendar?event=" in blob and "weekly-sets/15" in blob
    deadline = stored[group_calendar.google_event_id(f"deadline-{db.query(Assignment).first().id}")]
    assert deadline["start"] == {"date": "2026-09-17"}, "20:00 UTC is 01:00 on the 17th in Almaty"
    assert deadline["summary"] == "📝 Дедлайн: Essay до 01:00"

    google.calls.clear()
    assert group_calendar.sync(db, group, row, google, NOW) == "unchanged"
    assert google.calls == []

    second.start_datetime += timedelta(hours=2)
    second.end_datetime += timedelta(hours=2)
    first.is_active = False
    db.flush()
    assert group_calendar.sync(db, group, row, google, NOW) == "synced"
    assert google.names("events.update") == [("events.update", group_calendar.google_event_id(f"lesson-{second.id}"))]
    assert google.names("events.delete") == [("events.delete", group_calendar.google_event_id(f"lesson-{first.id}"))]


def test_run_once_paces_calendar_creation(live, monkeypatch):
    db, google = live["db"], FakeGoogle()
    for name in ("A", "B", "C"):
        live["at"](live["make"](name=name), 15)
    monkeypatch.setenv("GROUP_CALENDAR_CREATE_PER_RUN", "2")
    result = group_calendar.run_once(db, google, NOW)
    assert result["created"] == 2 and result["synced"] == 2
    assert group_calendar.run_once(db, google, NOW)["created"] == 1


def test_a_token_without_the_calendar_scope_pauses_quietly(live):
    db, google = live["db"], FakeGoogle()
    live["at"](live["make"](), 15)
    google.fail_insert_calendar = RuntimeError("invalid_scope: Bad Request")
    assert group_calendar.run_once(db, google, NOW)["skipped"] == "scope_missing"
    assert group_calendar.run_once(db, google, NOW + timedelta(minutes=5)) == {"skipped": "paused"}
    assert db.query(GroupGoogleCalendar).count() == 0


def test_a_spent_creation_quota_still_syncs_existing_calendars(live):
    db, google = live["db"], FakeGoogle()
    old = live["make"](name="Old")
    live["at"](old, 15)
    row = group_calendar.ensure_calendar(db, old, google)
    new = live["make"](name="New")
    live["at"](new, 15)
    google.fail_insert_calendar = FakeHttpError(403, '{"error": {"errors": [{"reason": "usageLimits"}]}}')
    result = group_calendar.run_once(db, google, NOW)
    assert result["synced"] == 1 and google.events_store[row.calendar_id]
    assert group_calendar._creation_paused_until == NOW + group_calendar.PAUSE


def test_subscribe_links(live):
    db, google = live["db"], FakeGoogle()
    group = live["make"]()
    links = group_calendar.subscribe_links(db, group)
    assert links["google_url"] is None
    assert links["ics_url"].endswith(f"/calendar/feeds/group/{group.id}-{group_calendar.group_sig(group.id)}.ics")
    row = group_calendar.ensure_calendar(db, group, google)
    url = group_calendar.subscribe_links(db, group)["google_url"]
    assert url.startswith("https://calendar.google.com/calendar/u/0?cid=") and "=" not in url.split("cid=")[1]
    import base64
    cid = url.split("cid=")[1]
    assert base64.urlsafe_b64decode(cid + "=" * (-len(cid) % 4)).decode() == row.calendar_id
    assert group_calendar.subscribe_links(db, None) is None


def test_the_hash_changes_only_with_content(live):
    group = live["make"]()
    live["at"](group, 15)
    items = calendar_items.group_items(live["db"], group, NOW)
    assert calendar_items.items_hash(items) == calendar_items.items_hash(list(reversed(items)))
