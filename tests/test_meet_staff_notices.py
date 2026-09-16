"""Staff hear about a lesson going wrong while it runs, from the Support bot, in the curators' topic.

Owner, 2026-09-16: 🔴 no recording (3 min), 🟠 teacher not in the room — saying outright whether it
records (5 min), ⚪ nobody came (10 min), and a 22:00 summary. Google and Support are faked; the
lesson, rosters, account links and notice rows are real.
"""
from datetime import datetime, time, timedelta

import pytest

from src.schemas.models import GoogleAccountLink, LessonRecording, MeetStaffNotice
from src.services import (google_workspace, group_bot_render, meet_presence, meet_recordings,
                          meet_staff_digest, meet_staff_notices, support_client)
from tests.test_operational_groups import db, world  # noqa: F401 - fixtures

TEACHER = "users/111"
STUDENT = "users/222"
CODE = "zhi-wyxo-icz"
SPACE = "spaces/lesson"
CALL = "conferenceRecords/c1"
# 18:00–19:00 Almaty
START = datetime(2026, 9, 16, 13, 0)


def _z(value: datetime) -> str:
    return value.isoformat() + "Z"


class FakeMeet:
    def __init__(self):
        self.calls = {}       # conference -> {"space", "start", "live"}
        self.people = {}      # conference -> [participant still in]
        self.recs = {}        # conference -> [recording]
        self._level = None

    def conferenceRecords(self):
        self._level = "conference"
        return self

    def participants(self):
        self._level = "participants"
        return self

    def recordings(self):
        self._level = "recordings"
        return self

    def list(self, pageSize=100, pageToken=None, filter=None, parent=None):
        if self._level == "participants":
            assert filter == "latest_end_time IS NULL"
            return _Call({"participants": self.people.get(parent, [])})
        if self._level == "recordings":
            return _Call({"recordings": self.recs.get(parent, [])})
        if filter == "end_time IS NULL":
            return _Call({"conferenceRecords": [{"name": n, "space": c["space"]}
                                                for n, c in self.calls.items() if c["live"]]})
        assert filter == f'space.meeting_code="{CODE}"'
        return _Call({"conferenceRecords": [{"name": n, "space": c["space"], "startTime": _z(c["start"]),
                                             **({"endTime": _z(c["end"])} if c.get("end") else {})}
                                            for n, c in self.calls.items()]})


class _Call:
    def __init__(self, body):
        self.body = body

    def execute(self):
        return self.body


@pytest.fixture
def lesson(world, monkeypatch):
    db = world["db"]
    world["teacher"].name = "Жанатбеккызы Лайла"
    world["teacher"].workspace_email = "laila@mastereducation.kz"
    group = world["group"](name="IELTS August 1 2026 - Лайла")
    world["enrol"](group)
    event = world["lesson"](group, title="IELTS August 1 2026 - Лайла: Lesson 26",
                            start_datetime=START, end_datetime=START + timedelta(hours=1),
                            meeting_url=f"https://meet.google.com/{CODE}")
    meet = FakeMeet()
    posted = []

    def call(method, path, *, actor_email, actor_name="", json_body=None, timeout=None, **_):
        assert (method, path) == ("POST", "/telegram/messages")
        posted.append(json_body)
        if fail_next:
            fail_next.pop()
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail="retry")
        return {"status": "sent", "telegram_message_id": 500 + len(posted)}

    fail_next = []
    monkeypatch.setattr(google_workspace, "meet_client", lambda: meet)
    monkeypatch.setattr(meet_recordings, "space_meet_code", lambda space: CODE if space == SPACE else None)
    monkeypatch.setattr(support_client, "call", call)
    monkeypatch.setenv("ENABLE_MEET_STAFF_NOTICES", "true")
    monkeypatch.setenv("MEET_NOTICES_SUPPORT_GROUP_ID", "210")
    monkeypatch.setenv("MEET_NOTICES_TOPIC_ID", "13771")
    meet_staff_notices._VISITED.clear()

    def in_room(*accounts, since=-5, live=True):
        meet.calls[CALL] = {"space": SPACE, "start": START + timedelta(minutes=min(since, -5)), "live": live}
        meet.people[CALL] = [{"signedinUser": {"user": a}, "earliestStartTime": _z(START + timedelta(minutes=since))}
                             for a in accounts]

    def joins(account, at):
        meet.people[CALL].append({"signedinUser": {"user": account},
                                  "earliestStartTime": _z(START + timedelta(minutes=at))})

    def recording_from(minute):
        meet.recs[CALL] = [{"state": "STARTED", "startTime": _z(START + timedelta(minutes=minute))}]

    def teacher_confirmed():
        db.add(GoogleAccountLink(google_user=TEACHER, user_id=world["teacher"].id))
        db.flush()

    def run(minute):
        return meet_staff_notices.run(db, now=START + timedelta(minutes=minute))

    return {"db": db, "event": event, "meet": meet, "posted": posted, "fail_next": fail_next, "run": run,
            "in_room": in_room, "joins": joins, "recording_from": recording_from,
            "teacher_confirmed": teacher_confirmed}


def texts(lesson):
    return [p["text"] for p in lesson["posted"]]


def test_teacher_in_the_room_without_a_recording_gets_one_red_notice_in_the_curators_topic(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT, TEACHER)

    assert lesson["run"](2) == {}
    lesson["run"](4)
    lesson["run"](5)

    assert len(lesson["posted"]) == 1
    body = lesson["posted"][0]
    assert body["telegram_group_id"] == 210 and body["message_thread_id"] == 13771
    assert body["idempotency_key"].startswith("meet-notice:")
    assert "Урок идёт без записи" in body["text"]
    assert "IELTS August 1 2026 - Лайла: Lesson 26" in body["text"] and "Жанатбеккызы Лайла" in body["text"]
    assert "18:00–19:00" in body["text"]
    assert "приложение Meet на телефоне или планшете" in body["text"]
    assert f"https://meet.google.com/{CODE}" in body["text"]


def test_no_recording_is_red_at_three_minutes_even_when_the_teachers_work_account_is_missing(lesson):
    # A teacher who cannot get into Workspace yet teaches from a personal account; staff join to record.
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT)

    lesson["run"](4)
    assert len(lesson["posted"]) == 1
    red = lesson["posted"][0]["text"]
    assert "Урок идёт без записи" in red and "Рабочего аккаунта учителя в комнате нет" in red

    lesson["run"](6)
    lesson["run"](8)
    assert len(lesson["posted"]) == 2  # the teacher question is its own message
    orange = lesson["posted"][1]["text"]
    assert "Учитель не зашёл в урок" in orange and "Урок не записывается" in orange
    assert "зашёл с другого (личного) аккаунта" in orange


def test_staff_joining_to_record_resolves_red_while_the_teacher_is_still_missing(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT)
    lesson["run"](4)
    lesson["joins"]("users/staff", at=5)
    lesson["recording_from"](5)
    lesson["run"](6)

    assert "Запись началась в 18:05" in lesson["posted"][1]["text"]
    assert lesson["posted"][1]["reply_to_message_id"] == 501
    orange = lesson["posted"][2]["text"]
    assert "Учитель не зашёл в урок" in orange and "✅ Запись идёт." in orange


def test_teacher_arriving_on_the_ipad_app_resolves_orange_and_red_stays_open(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT)
    lesson["run"](6)  # both due in one tick: red, then orange
    assert [p["text"].split("\n")[0] for p in lesson["posted"]] == ["🔴 <b>Урок идёт без записи</b>",
                                                                     "🟠 <b>Учитель не зашёл в урок</b>"]
    orange_id = 502

    lesson["joins"](TEACHER, at=8)
    lesson["run"](8.5)
    assert len(lesson["posted"]) == 2  # not a minute in the room yet
    lesson["run"](9)

    reply = lesson["posted"][2]
    assert reply["reply_to_message_id"] == orange_id and reply["message_thread_id"] == 13771
    assert "Учитель зашёл в 18:08" in reply["text"] and "Записи всё ещё нет" in reply["text"]

    lesson["run"](10)
    assert len(lesson["posted"]) == 3  # red is already out, and still unresolved


def test_a_recording_that_starts_after_the_red_notice_gets_a_reply(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT, TEACHER)
    lesson["run"](4)
    lesson["recording_from"](6)
    lesson["run"](7)
    lesson["run"](8)

    assert len(lesson["posted"]) == 2
    assert lesson["posted"][1]["reply_to_message_id"] == 501
    assert "Запись началась в 18:06 (6 мин. от начала урока)" in lesson["posted"][1]["text"]


def test_unconfirmed_teacher_accounts_mean_red_with_a_hint_and_never_orange(lesson):
    lesson["in_room"](STUDENT, TEACHER)
    for minute in (4, 6, 8):
        lesson["run"](minute)
    assert len(lesson["posted"]) == 1
    assert "не подтверждены" in lesson["posted"][0]["text"]


def test_a_recording_lesson_is_quiet(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT, TEACHER)
    lesson["recording_from"](0)
    for minute in (4, 6, 12):
        lesson["run"](minute)
    assert lesson["posted"] == []


def test_nobody_in_the_room_at_ten_minutes_is_white_but_people_who_came_and_left_are_not(lesson):
    lesson["run"](9)
    assert lesson["posted"] == []
    lesson["run"](11)
    lesson["run"](12)
    assert len(lesson["posted"]) == 1 and "В уроке никого нет" in lesson["posted"][0]["text"]


def test_a_room_people_visited_and_left_is_not_empty(lesson):
    lesson["in_room"](STUDENT, since=-3, live=False)  # an ended call from just before the lesson
    lesson["meet"].people.clear()
    lesson["run"](11)
    assert lesson["posted"] == []


def test_a_call_opened_long_before_the_lesson_and_left_during_it_is_not_an_empty_room(lesson):
    # 16.09, Abzal's 20:30: students opened the room at 19:52 and that one call ran the lesson.
    lesson["meet"].calls["conferenceRecords/early"] = {
        "space": SPACE, "start": START - timedelta(minutes=38), "end": START + timedelta(minutes=5), "live": False}
    lesson["run"](11)
    assert lesson["posted"] == []


def test_a_call_that_ended_well_before_the_lesson_does_not_count_as_a_visit(lesson):
    lesson["meet"].calls["conferenceRecords/morning"] = {
        "space": SPACE, "start": START - timedelta(hours=5), "end": START - timedelta(hours=4), "live": False}
    lesson["run"](11)
    assert "В уроке никого нет" in lesson["posted"][0]["text"]


def test_a_failed_send_is_retried_under_the_same_key(lesson):
    lesson["teacher_confirmed"]()
    lesson["in_room"](STUDENT, TEACHER)
    lesson["fail_next"].append(True)
    lesson["run"](4)
    lesson["run"](5)

    assert len(lesson["posted"]) == 2
    assert lesson["posted"][0]["idempotency_key"] == lesson["posted"][1]["idempotency_key"]
    notice = lesson["db"].query(MeetStaffNotice).filter_by(event_id=lesson["event"].id).one()
    assert (notice.kind, notice.status, notice.attempts) == ("no_recording", "sent", 2)


def test_after_the_lesson_nothing_new_is_raised(lesson):
    lesson["in_room"](STUDENT, TEACHER)
    lesson["run"](65)
    assert lesson["posted"] == []


def test_switched_off_it_does_not_ask_google(lesson, monkeypatch):
    monkeypatch.setattr(google_workspace, "meet_client", lambda: pytest.fail("must not call Google"))
    monkeypatch.setenv("ENABLE_MEET_STAFF_NOTICES", "false")
    assert lesson["run"](11) == {}
    monkeypatch.setenv("ENABLE_MEET_STAFF_NOTICES", "true")
    monkeypatch.delenv("MEET_NOTICES_SUPPORT_GROUP_ID")
    assert lesson["run"](11) == {}


# --- the 22:00 summary -------------------------------------------------------------------------


def _almaty(clock: time) -> datetime:
    return datetime.combine(group_bot_render.local(START).date(), clock) - group_bot_render.ALMATY_OFFSET


def test_the_summary_goes_out_once_at_22_with_what_went_wrong(lesson, world, monkeypatch):
    db = lesson["db"]
    group2 = world["group"](name="SAT July 3 - Лайла")
    world["enrol"](group2)
    recorded = world["lesson"](group2, title="SAT July 3 - Лайла: Lesson 9", start_datetime=START + timedelta(hours=1),
                               end_datetime=START + timedelta(hours=2), meeting_url="https://meet.google.com/aaa-bbbb-ccc")
    db.add(LessonRecording(event_id=recorded.id, status="ready"))
    lesson["in_room"](STUDENT, TEACHER, live=False)  # lesson 26: a call, no recording
    db.add(MeetStaffNotice(kind="no_recording", event_id=lesson["event"].id, status="sent", attempts=1,
                           created_at=START + timedelta(minutes=4), resolved_at=START + timedelta(minutes=9)))
    db.flush()

    def records(_db, events, now):
        return [{"event_id": e.id, "state": "ready",
                 "teacher": {"name": "Жанатбеккызы Лайла",
                             "flags": [{"code": "teacher_late", "minutes": 7}] if e.id == recorded.id else []}}
                for e in events]

    monkeypatch.setattr(meet_presence, "records", records)

    assert meet_staff_digest.send_if_due(db, _almaty(time(21, 59))) is None
    assert meet_staff_digest.send_if_due(db, _almaty(time(22, 1))) == "sent"
    assert meet_staff_digest.send_if_due(db, _almaty(time(22, 2))) is None

    assert len(lesson["posted"]) == 1
    body = lesson["posted"][0]
    assert body["message_thread_id"] == 13771 and body["idempotency_key"].startswith("meet-digest:")
    text = body["text"]
    assert "Meet — итоги дня" in text
    assert "Уроков в Meet: 2 · с записью 1 · без записи 1" in text
    assert "❌ <b>Без записи</b>\n• 18:00 IELTS August 1 2026 - Лайла: Lesson 26 — Жанатбеккызы Лайла" in text
    assert "⏰ <b>Учитель опоздал</b>\n• 19:00 SAT July 3 - Лайла: Lesson 9 — Жанатбеккызы Лайла, на 7 мин." in text
    assert "Сигналы за день: 🔴 1 (решено 1)" in text


def test_the_summary_counts_a_recorded_call_that_opened_long_before_the_lesson(lesson, monkeypatch):
    lesson["meet"].calls[CALL] = {"space": SPACE, "start": START - timedelta(minutes=38),
                                  "end": START + timedelta(minutes=67), "live": False}
    lesson["recording_from"](-3)
    monkeypatch.setattr(meet_presence, "records", lambda _db, events, now: [])

    assert meet_staff_digest.send_if_due(lesson["db"], _almaty(time(22, 1))) == "sent"
    text = lesson["posted"][0]["text"]
    assert "Уроков в Meet: 1 · с записью 1 · без записи 0" in text
    assert "Никто не заходил" not in text


def test_a_day_without_meet_lessons_sends_no_summary(world, monkeypatch):
    posted = []
    monkeypatch.setattr(support_client, "call", lambda *a, **k: posted.append(k) or {})
    monkeypatch.setenv("ENABLE_MEET_STAFF_NOTICES", "true")
    monkeypatch.setenv("MEET_NOTICES_SUPPORT_GROUP_ID", "210")
    now = datetime(2030, 1, 5, 17, 30)  # 22:30 Almaty, nothing scheduled
    assert meet_staff_digest.send_if_due(world["db"], now) == "skipped"
    assert meet_staff_digest.send_if_due(world["db"], now) is None
    assert posted == []
