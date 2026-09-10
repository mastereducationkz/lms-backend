"""A lesson's Meet record, read back as people — and the flags the owner approved (2026-09-11).

Against a real database, because the record is assembled from five tables and the access
rule is an SQL clause. Times are minutes from the lesson's start; the lesson runs 60 minutes.
"""
from datetime import datetime, timedelta
from itertools import count

import pytest
from fastapi import HTTPException

from src.events.routes.meet_attendance import (
    IdentityIn,
    confirm_identity,
    get_lesson_record,
    list_lesson_records,
)
from src.schemas.models import (
    Attendance,
    GoogleAccountLink,
    MeetConference,
    MeetParticipant,
    MeetParticipantSession,
)
from src.services import meet_presence
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

_names = count()


def _named(db, user, name):
    user.name = name
    db.flush()
    return user


@pytest.fixture
def room(world):
    """A finished lesson of one group: a teacher, three students, one Meet call."""
    db = world["db"]
    teacher = _named(db, world["teacher"], "Гульзада Сапарова")
    group = world["group"](name="August 19 SAT - Gulzada")
    aya = _named(db, world["enrol"](group), "Аяулым Сейтова")
    eldana = _named(db, world["enrol"](group), "Елдана Нұрлан")
    shyngys = _named(db, world["enrol"](group), "Шыңғыс Бек")
    lesson = world["lesson"](group, days_ahead=-1)
    start = lesson.start_datetime
    call = MeetConference(event_id=lesson.id, conference_record=f"conferenceRecords/{lesson.id}-{next(_names)}",
                          started_at=start - timedelta(minutes=10), ended_at=start + timedelta(minutes=65),
                          synced_at=start + timedelta(minutes=70))
    db.add(call)
    db.flush()

    def joined(display_name, *spans, account="auto", kind="signed_in", conference=call):
        if account == "auto":
            account = f"users/{next(_names)}" if kind == "signed_in" else None
        p = MeetParticipant(conference_id=conference.id, event_id=conference.event_id,
                            participant_name=f"{conference.conference_record}/participants/{next(_names)}",
                            kind=kind, google_user=account, display_name=display_name)
        db.add(p)
        db.flush()
        for i, (a, b) in enumerate(spans):
            db.add(MeetParticipantSession(
                participant_id=p.id, session_name=f"{p.participant_name}/participantSessions/{i}",
                joined_at=start + timedelta(minutes=a), left_at=start + timedelta(minutes=b)))
        db.flush()
        return p

    def link(participant, user):
        db.add(GoogleAccountLink(google_user=participant.google_user, user_id=user.id))
        db.flush()

    def mark(user, status):
        db.add(Attendance(event_id=lesson.id, user_id=user.id, status=status))
        db.flush()

    return {"db": db, "world": world, "lesson": lesson, "call": call, "group": group, "teacher": teacher,
            "aya": aya, "eldana": eldana, "shyngys": shyngys, "joined": joined, "link": link, "mark": mark,
            "after": lesson.end_datetime + timedelta(hours=1)}


def _record(room, now=None):
    return meet_presence.lesson(room["db"], room["lesson"], now or room["after"])


def _student(record, user):
    return next(s for s in record["students"] if s["user_id"] == user.id)


def _codes(person):
    return {f["code"]: f.get("minutes") for f in person["flags"]}


# ── when the record is judged ────────────────────────────────────────────────────────────

def test_nothing_is_judged_before_or_just_after_the_lesson(room):
    lesson = room["lesson"]
    assert _record(room, lesson.start_datetime - timedelta(minutes=1))["state"] == "not_started"
    assert _record(room, lesson.end_datetime + timedelta(minutes=10))["state"] == "waiting"
    assert _record(room)["state"] == "ready"


def test_a_lesson_whose_room_never_opened_says_so(room):
    db, world = room["db"], room["world"]
    room["teacher"].workspace_email = "gulzada@mastereducation.kz"
    quiet = world["lesson"](room["group"], days_ahead=-2, meeting_url="https://meet.google.com/abc-defg-hij")
    assert meet_presence.lesson(db, quiet, quiet.end_datetime + timedelta(hours=1))["state"] == "none"
    much_later = quiet.end_datetime + meet_presence.GOOGLE_KEEPS + timedelta(days=1)
    assert meet_presence.lesson(db, quiet, much_later)["state"] == "unavailable"


def test_a_lesson_in_a_teachers_own_meet_room_says_nothing(room):
    db, world = room["db"], room["world"]
    own_link = world["lesson"](room["group"], days_ahead=-2, meeting_url="https://meet.google.com/own-link-xyz")
    for moment in (own_link.end_datetime + timedelta(minutes=5), own_link.end_datetime + timedelta(hours=1)):
        assert meet_presence.lesson(db, own_link, moment)["state"] == "no_room", "no Workspace teacher, no record"


def test_a_call_google_has_not_handed_over_holds_the_record_back_for_a_while(room):
    db = room["db"]
    db.add(MeetConference(event_id=room["lesson"].id, conference_record=f"conferenceRecords/late-{next(_names)}",
                          started_at=room["lesson"].start_datetime, ended_at=room["lesson"].end_datetime))
    db.flush()
    assert _record(room)["state"] == "waiting"
    later = _record(room, room["lesson"].end_datetime + meet_presence.GIVE_UP_WAITING_AFTER + timedelta(minutes=1))
    assert later["state"] == "ready" and later["partial"] is True


# ── lateness ─────────────────────────────────────────────────────────────────────────────

def test_late_students_and_a_late_teacher_who_ended_early(room):
    room["link"](room["joined"]("Gulzada", (4, 52)), room["teacher"])
    room["link"](room["joined"]("Aya", (7, 45)), room["aya"])
    room["link"](room["joined"]("Eldana", (-8, 61)), room["eldana"])

    record = _record(room)
    assert _codes(record["teacher"]) == {"teacher_late": 4, "ended_early": 8}
    assert _codes(_student(record, room["aya"])) == {"late": 7, "left_early": 15}
    assert _codes(_student(record, room["eldana"])) == {}, "early and to the end is simply on time"
    assert _student(record, room["eldana"])["minutes_in_lesson"] == 60


def test_the_thresholds_are_strict(room):
    room["link"](room["joined"]("Gulzada", (2, 55)), room["teacher"])
    room["link"](room["joined"]("Aya", (5, 50)), room["aya"])
    record = _record(room)
    assert _codes(record["teacher"]) == {}, "2 min late and 5 min early are within the rules"
    assert _codes(_student(record, room["aya"])) == {}, "5 min late and 10 min early are within the rules"


# ── marks against the room ───────────────────────────────────────────────────────────────

def test_marked_present_but_never_in_the_room(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["mark"](room["aya"], "present")
    record = _record(room)
    assert "marked_present_not_joined" in _codes(_student(record, room["aya"]))
    assert record["mismatches"] == 1
    assert record["flags"][0]["name"] == "Аяулым Сейтова"


def test_an_unconfirmed_account_holds_never_joined_back(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["mark"](room["aya"], "present")
    stranger = room["joined"]("iPhone 13", (1, 60))

    record = _record(room)
    assert record["held_back"] is True
    assert "marked_present_not_joined" not in _codes(_student(record, room["aya"])), "that iPhone may be her"
    assert record["unknown"][0]["participant_id"] == stranger.id

    room["link"](stranger, room["eldana"])
    assert "marked_present_not_joined" in _codes(_student(_record(room), room["aya"]))


def test_marked_absent_but_in_the_room_ten_minutes_or_more(room):
    room["link"](room["joined"]("Aya", (0, 15)), room["aya"])
    room["link"](room["joined"]("Eldana", (0, 5)), room["eldana"])
    room["mark"](room["aya"], "absent")
    room["mark"](room["eldana"], "absent")
    record = _record(room)
    assert _codes(_student(record, room["aya"]))["marked_absent_was_in_room"] == 15
    assert "marked_absent_was_in_room" not in _codes(_student(record, room["eldana"]))


def test_the_teacher_who_never_came(room):
    room["link"](room["joined"]("Aya", (0, 60)), room["aya"])
    assert _codes(_record(room)["teacher"]) == {"teacher_not_joined": None}


def test_legacy_statuses_read_as_marks(room):
    room["mark"](room["aya"], "1")
    room["mark"](room["eldana"], "missed")
    record = _record(room)
    assert _student(record, room["aya"])["mark"] == "present"
    assert _student(record, room["eldana"])["mark"] == "absent"


def test_a_student_taken_off_the_lesson_is_not_listed(room):
    room["mark"](room["shyngys"], "removed")
    assert room["shyngys"].id not in {s["user_id"] for s in _record(room)["students"]}


# ── who is who ───────────────────────────────────────────────────────────────────────────

def test_a_guest_and_a_signed_in_account_of_one_student_are_one_person(room):
    guest = room["joined"]("Аяу", (0, 7), kind="guest")
    guest.lesson_user_id = room["aya"].id
    room["link"](room["joined"]("Aya", (7.3, 60)), room["aya"])
    aya = _student(_record(room), room["aya"])
    assert aya["joins"] == 2 and len(aya["accounts"]) == 2
    assert aya["minutes_in_lesson"] == 59
    assert _codes(aya) == {}


def test_overlapping_sessions_show_as_one_stretch(room):
    room["link"](room["joined"]("Gulzada", (-6, 0), (1, 63), (5, 58)), room["teacher"])
    teacher = _record(room)["teacher"]
    assert teacher["joins"] == 3
    assert len(teacher["sessions"]) == 2


def test_a_morning_test_call_in_the_same_room_is_not_the_lesson(room):
    room["link"](room["joined"]("Aya", (-300, -290)), room["aya"])
    room["mark"](room["aya"], "present")
    aya = _student(_record(room), room["aya"])
    assert aya["sessions"] == []
    assert "marked_present_not_joined" in _codes(aya)


def test_accounts_that_are_not_students_are_set_apart_and_hold_nothing_back(room):
    observer = room["joined"]("Fikrat", (0, 60))
    room["db"].add(GoogleAccountLink(google_user=observer.google_user, not_a_student=True))
    room["mark"](room["aya"], "present")
    record = _record(room)
    assert [p["participant_id"] for p in record["not_tracked"]] == [observer.id]
    assert record["held_back"] is False
    assert "marked_present_not_joined" in _codes(_student(record, room["aya"]))


def test_a_student_of_another_group_is_listed_under_others(room):
    stranger = _named(room["db"], _user(room["db"], "student"), "Мадина Ким")
    room["link"](room["joined"]("Madina", (0, 60)), stranger)
    record = _record(room)
    assert [p["user_id"] for p in record["others"]] == [stranger.id]
    assert stranger.id not in {s["user_id"] for s in record["students"]}


# ── suggestions ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("shown, known", [
    ("Aya", "Аяулым Сейтова"),
    ("Shyngys Bek", "Шыңғыс Бек"),
    ("Sherkhan", "Шерхан Әли"),
    ("Zhansaya", "Жансая Тимур"),
    ("Aisha", "Айша Қайрат"),
])
def test_latin_and_cyrillic_spellings_meet(shown, known):
    assert meet_presence.name_score(shown, known) >= meet_presence.SUGGEST_AT


def test_a_suggestion_needs_one_clear_winner():
    two = [{"user_id": 1, "name": "Аружан Ким"}, {"user_id": 2, "name": "Аружан Сейт"}]
    assert meet_presence.suggest("Aruzhan", two) is None
    assert meet_presence.suggest("Aruzhan Seit", two) == {"user_id": 2, "name": "Аружан Сейт"}
    assert meet_presence.suggest("med", two) is None


def test_the_record_suggests_who_an_unknown_account_is(room):
    room["joined"]("Shyngys", (0, 60))
    assert _record(room)["unknown"][0]["suggestion"] == {"user_id": room["shyngys"].id, "name": "Шыңғыс Бек"}


# ── confirming, through the route ────────────────────────────────────────────────────────

def test_confirming_an_account_once_reaches_every_lesson(room):
    db, world = room["db"], room["world"]
    account = room["joined"]("Shyngys", (0, 60))
    record = confirm_identity(account.id, IdentityIn(user_id=room["shyngys"].id), db=db, current_user=room["teacher"])
    assert record["state"] == "ready" and record["unknown"] == []

    next_lesson = world["lesson"](room["group"], days_ahead=-0.5)
    call = MeetConference(event_id=next_lesson.id, conference_record=f"conferenceRecords/n-{next(_names)}",
                          ended_at=next_lesson.end_datetime, synced_at=next_lesson.end_datetime)
    db.add(call)
    db.flush()
    again = MeetParticipant(conference_id=call.id, event_id=next_lesson.id, kind="signed_in",
                            participant_name=f"{call.conference_record}/participants/1",
                            google_user=account.google_user, display_name="Shyngys")
    db.add(again)
    db.flush()
    db.add(MeetParticipantSession(participant_id=again.id, session_name=f"{again.participant_name}/s/1",
                                  joined_at=next_lesson.start_datetime, left_at=next_lesson.end_datetime))
    db.flush()
    later = meet_presence.lesson(db, next_lesson, next_lesson.end_datetime + timedelta(hours=1))
    assert _student(later, room["shyngys"])["minutes_in_lesson"] == 60
    assert later["unknown"] == []


def test_a_guest_is_matched_for_this_lesson_only(room):
    guest = room["joined"]("Аяу", (0, 60), kind="guest")
    confirm_identity(guest.id, IdentityIn(user_id=room["aya"].id), db=room["db"], current_user=room["teacher"])
    assert guest.lesson_user_id == room["aya"].id
    assert room["db"].query(GoogleAccountLink).count() == 0, "nothing to remember a guest by"


def test_not_a_student_and_undo(room):
    db = room["db"]
    account = room["joined"]("Fikrat", (0, 60))
    record = confirm_identity(account.id, IdentityIn(not_a_student=True), db=db, current_user=room["teacher"])
    assert [p["participant_id"] for p in record["not_tracked"]] == [account.id]
    record = confirm_identity(account.id, IdentityIn(), db=db, current_user=room["teacher"])
    assert [p["participant_id"] for p in record["unknown"]] == [account.id]
    assert db.get(GoogleAccountLink, account.google_user) is None


def test_only_this_lessons_people_can_be_chosen(room):
    account = room["joined"]("Someone", (0, 60))
    outsider = _user(room["db"], "student")
    for body in (IdentityIn(user_id=outsider.id), IdentityIn(user_id=room["aya"].id, not_a_student=True)):
        with pytest.raises(HTTPException) as err:
            confirm_identity(account.id, body, db=room["db"], current_user=room["teacher"])
        assert err.value.status_code == 422


# ── who may read it ──────────────────────────────────────────────────────────────────────

def test_students_and_strangers_get_not_found(room):
    db, lesson = room["db"], room["lesson"]
    other_curator = _user(db, "curator")
    for user in (room["aya"], other_curator, _user(db, "teacher")):
        with pytest.raises(HTTPException) as err:
            get_lesson_record(lesson.id, db=db, current_user=user)
        assert err.value.status_code == 404
    account = room["joined"]("Aya", (0, 60))
    with pytest.raises(HTTPException) as err:
        confirm_identity(account.id, IdentityIn(user_id=room["aya"].id), db=db, current_user=room["aya"])
    assert err.value.status_code == 404


def test_the_teacher_the_groups_curator_and_the_heads_can_read_it(room):
    db, lesson = room["db"], room["lesson"]
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    for user in (room["teacher"], curator, _user(db, "head_teacher"), _user(db, "head_curator"), _user(db, "admin")):
        assert get_lesson_record(lesson.id, db=db, current_user=user)["event_id"] == lesson.id


# ── the list: review screen and journal dots ─────────────────────────────────────────────

def _list(db, user, **kw):
    params = dict(date_from=None, date_to=None, teacher_id=None, group_id=None)
    params.update(kw)
    return list_lesson_records(db=db, current_user=user, **params)["items"]


def test_the_list_shows_recorded_lessons_with_their_flags(room):
    db, world = room["db"], room["world"]
    room["link"](room["joined"]("Gulzada", (4, 60)), room["teacher"])
    room["mark"](room["aya"], "present")
    world["lesson"](room["group"], days_ahead=-3)  # no record: not listed
    items = _list(db, _user(db, "admin"))
    ours = [i for i in items if i["event_id"] == room["lesson"].id]
    assert len(ours) == 1
    item = ours[0]
    assert item["groups"] == [{"id": room["group"].id, "name": "August 19 SAT - Gulzada"}]
    assert item["teacher"]["name"] == "Гульзада Сапарова"
    assert {f["code"] for f in item["flags"]} == {"teacher_late", "marked_present_not_joined"}
    assert item["students"] == 3 and item["joined"] == 0 and item["mismatches"] == 1
    assert item["start"].endswith("Z")


def test_the_list_filters_by_group_and_respects_access(room):
    db, world = room["db"], room["world"]
    other = world["group"](name="IELTS June 14 2026")
    assert all(i["event_id"] != room["lesson"].id
               for i in _list(db, _user(db, "admin"), group_id=other.id))
    assert _list(db, room["aya"]) == []
    assert _list(db, _user(db, "curator")) == []
