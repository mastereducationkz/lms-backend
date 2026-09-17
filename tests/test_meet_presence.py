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
from src.utils.utc_json import utc_z
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


def test_short_calls_around_the_lesson_do_not_make_it_judged(room):
    """2026-09-15: teachers checked their rooms at 18:58, the worker stalled right after saving those
    few-second calls, and taught, recorded lessons read "Teacher never joined" for everyone."""
    db, world = room["db"], room["world"]
    room["teacher"].workspace_email = "gulzada@mastereducation.kz"
    taught = world["lesson"](room["group"], days_ahead=-2, meeting_url="https://meet.google.com/tau-ghtx-now")
    start, end = taught.start_datetime, taught.end_datetime
    for a, b in ((-2, -1.9), (-0.5, 0.03)):  # a five-second check; one that ran 1.5 s past the start
        db.add(MeetConference(event_id=taught.id, conference_record=f"conferenceRecords/check-{next(_names)}",
                              started_at=start + timedelta(minutes=a), ended_at=start + timedelta(minutes=b),
                              synced_at=start + timedelta(minutes=10)))
    db.add(Attendance(event_id=taught.id, user_id=room["aya"].id, status="present"))
    db.flush()

    record = meet_presence.lesson(db, taught, end + timedelta(hours=1))
    assert record["state"] == "waiting", "the lesson's own call has not been saved yet"
    assert not record.get("flags")

    # Once the lesson's own call is saved, it is judged as usual.
    db.add(MeetConference(event_id=taught.id, conference_record=f"conferenceRecords/lesson-{next(_names)}",
                          started_at=start + timedelta(minutes=1), ended_at=end,
                          synced_at=end + timedelta(minutes=10)))
    db.flush()
    assert meet_presence.lesson(db, taught, end + timedelta(hours=1))["state"] == "ready"


def test_a_room_with_only_calls_around_the_lesson_is_judged_after_waiting_long_enough(room):
    db, world = room["db"], room["world"]
    room["teacher"].workspace_email = "gulzada@mastereducation.kz"
    skipped = world["lesson"](room["group"], days_ahead=-2, meeting_url="https://meet.google.com/ski-pped-now")
    start = skipped.start_datetime
    db.add(MeetConference(event_id=skipped.id, conference_record=f"conferenceRecords/early-{next(_names)}",
                          started_at=start - timedelta(minutes=20), ended_at=start - timedelta(minutes=15),
                          synced_at=start))
    db.flush()
    later = skipped.end_datetime + meet_presence.GIVE_UP_WAITING_AFTER + timedelta(minutes=1)
    assert meet_presence.lesson(db, skipped, later)["state"] == "ready"


def test_a_waiting_lesson_says_what_it_is_waiting_for(room):
    """2026-09-15: «Loading» read as a slow LMS. A waiting record now says which stage it is in."""
    db, world, lesson = room["db"], room["world"], room["lesson"]
    assert _record(room, lesson.start_datetime + timedelta(minutes=10))["waiting"]["stage"] == "lesson_running"
    settling = _record(room, lesson.end_datetime + timedelta(minutes=10))["waiting"]
    assert settling["stage"] == "settling" and [c["lesson_call"] for c in settling["calls"]] == [True]
    assert "waiting" not in _record(room), "a judged record waits for nothing"

    room["teacher"].workspace_email = "gulzada@mastereducation.kz"
    taught = world["lesson"](room["group"], days_ahead=-2, meeting_url="https://meet.google.com/wai-tingn-owx")
    start, end = taught.start_datetime, taught.end_datetime
    db.add(MeetConference(event_id=taught.id, conference_record=f"conferenceRecords/check-{next(_names)}",
                          started_at=start - timedelta(minutes=2), ended_at=start - timedelta(minutes=1),
                          synced_at=start))
    db.flush()
    an_hour_on = end + timedelta(hours=1)
    waiting = meet_presence.lesson(db, taught, an_hour_on)["waiting"]
    assert waiting["stage"] == "awaiting_google"
    assert [(c["saved"], c["lesson_call"]) for c in waiting["calls"]] == [(True, False)], "a room check, not the lesson"
    assert waiting["judge_at"] == utc_z(end + meet_presence.GIVE_UP_WAITING_AFTER)

    call = MeetConference(event_id=taught.id, conference_record=f"conferenceRecords/lesson-{next(_names)}",
                          started_at=start, ended_at=None)
    db.add(call)
    db.flush()
    assert meet_presence.lesson(db, taught, an_hour_on)["waiting"]["stage"] == "call_open"
    call.ended_at = end
    db.flush()
    assert meet_presence.lesson(db, taught, an_hour_on)["waiting"]["stage"] == "collecting"


# ── lateness ─────────────────────────────────────────────────────────────────────────────

def test_late_students_on_the_teachers_clock_and_a_late_teacher_who_ended_early(room):
    """Students are timed from when the teacher came to when the teacher left (owner, 2026-09-16)."""
    room["link"](room["joined"]("Gulzada", (4, 52)), room["teacher"])
    room["link"](room["joined"]("Aya", (11, 38)), room["aya"])
    room["link"](room["joined"]("Eldana", (-8, 61)), room["eldana"])
    room["link"](room["joined"]("Шыңғыс", (7, 45)), room["shyngys"])

    record = _record(room)
    assert _codes(record["teacher"]) == {"teacher_late": 4, "ended_early": 8}
    assert _codes(_student(record, room["aya"])) == {"late": 7, "left_early": 14}
    assert _codes(_student(record, room["eldana"])) == {}, "early and to the end is simply on time"
    assert _codes(_student(record, room["shyngys"])) == {}, "3 minutes after the teacher, 7 before the teacher left"
    assert _student(record, room["eldana"])["minutes_in_lesson"] == 60


def test_the_thresholds_count_whole_minutes(room):
    room["link"](room["joined"]("Gulzada", (2, 55)), room["teacher"])
    room["link"](room["joined"]("Aya", (2 + 5 + 59 / 60, 55 - 10 - 59 / 60)), room["aya"])
    room["link"](room["joined"]("Eldana", (8, 44)), room["eldana"])
    record = _record(room)
    assert _codes(record["teacher"]) == {}, "2 min late and 5 min early are within the rules"
    assert _codes(_student(record, room["aya"])) == {}, "5:59 late and 10:59 early are within the rules"
    assert _codes(_student(record, room["eldana"])) == {"late": 6, "left_early": 11}


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


def test_marked_absent_but_in_the_lesson_three_quarters_of_it(room):
    """Was «10 minutes or more»; since 2026-09-16 it is Meet's verdict that disagrees: 45 of 60."""
    room["link"](room["joined"]("Aya", (0, 45)), room["aya"])
    room["link"](room["joined"]("Eldana", (0, 44)), room["eldana"])
    room["mark"](room["aya"], "absent")
    room["mark"](room["eldana"], "absent")
    record = _record(room)
    assert _codes(_student(record, room["aya"]))["marked_absent_was_in_room"] == 45
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


def test_teachers_and_curators_list_their_own_lessons_and_nobody_elses(room):
    """The page opened to teachers and curators on 2026-09-11; the backend scope is what keeps
    it theirs — the teacher who taught or owns the group, the group's curator."""
    db = room["db"]
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    for viewer in (room["teacher"], curator):
        assert room["lesson"].id in {i["event_id"] for i in _list(db, viewer)}
    assert _list(db, _user(db, "teacher")) == [], "another teacher's lessons stay theirs"


def test_a_lesson_under_way_is_listed_before_any_call_is_saved(room):
    """2026-09-17: «Indi Maria SAT 2026: Lesson 8» ran 15:00–16:00 with two people in the room and was
    nowhere on the page — calls are saved only after they end, and the list wanted a saved call."""
    db, world = room["db"], room["world"]
    room["teacher"].workspace_email = "gulzada@mastereducation.kz"
    minutes = 1 / (24 * 60)
    running = world["lesson"](room["group"], days_ahead=-10 * minutes, meeting_url="https://meet.google.com/run-ning-now")
    handed_over = world["lesson"](room["group"], days_ahead=-70 * minutes, meeting_url="https://meet.google.com/col-lect-ing")
    db.add(MeetConference(event_id=handed_over.id, conference_record=f"conferenceRecords/collect-{next(_names)}",
                          started_at=handed_over.start_datetime,
                          ended_at=handed_over.end_datetime + timedelta(minutes=3)))  # not saved yet
    own_link = world["lesson"](room["group"], days_ahead=-10 * minutes)  # a teacher's own Meet link
    no_call = world["lesson"](room["group"], days_ahead=-180 * minutes, meeting_url="https://meet.google.com/gon-eeee-now")
    db.flush()

    items = {i["event_id"]: i for i in _list(db, _user(db, "admin"))}
    assert (items[running.id]["state"], items[running.id]["waiting"]["stage"]) == ("waiting", "lesson_running")
    assert items[handed_over.id]["waiting"]["stage"] == "collecting"
    assert own_link.id not in items, "no LMS room: nothing will ever come"
    assert no_call.id not in items, "over hours ago with no call: not a Meet record"
    assert items[room["lesson"].id]["state"] == "ready", "lessons with a saved call are listed as before"


# ── the class list, with or without Meet data (watch pages, 2026-09-11) ──────────────────

def test_without_a_meet_record_the_class_list_and_marks_are_still_there(room):
    db, world = room["db"], room["world"]
    quiet = world["lesson"](room["group"], days_ahead=-2)
    db.add(meet_presence.Attendance(event_id=quiet.id, user_id=room["aya"].id, status="present"))
    db.add(meet_presence.Attendance(event_id=quiet.id, user_id=room["shyngys"].id, status="removed"))
    db.flush()
    record = meet_presence.lesson(db, quiet, quiet.end_datetime + timedelta(hours=1))
    assert record["state"] == "no_room"
    assert [(r["name"], r["mark"]) for r in record["roster"]] == [
        ("Аяулым Сейтова", "present"), ("Елдана Нұрлан", None)], "taken-off students and the teacher are not the class"


def test_the_public_view_shows_people_and_times_and_nothing_to_act_on(room):
    room["link"](room["joined"]("Gulzada", (4, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (7, 60)), room["aya"])
    room["joined"]("iPhone 13", (1, 60))
    room["mark"](room["eldana"], "present")

    view = meet_presence.public_participants(_record(room))
    assert view["state"] == "ready" and view["held_back"] is True
    assert view["teacher"]["name"] == "Гульзада Сапарова"
    assert {f["code"] for f in view["teacher"]["flags"]} == {"teacher_late"}
    aya = next(s for s in view["students"] if s["name"] == "Аяулым Сейтова")
    assert aya["first_join"].endswith("Z") and aya["minutes_in_lesson"] == 53
    assert [u["display_name"] for u in view["unknown"]] == ["iPhone 13"]
    flat = repr(view)
    for private in ("participant_id", "user_id", "google_user", "candidates", "suggestion", "users/"):
        assert private not in flat, private
