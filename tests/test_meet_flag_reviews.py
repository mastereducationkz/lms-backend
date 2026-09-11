"""Reviewing a Meet flag takes it out of «Needs attention» — with a reason (owner, 2026-09-11).

A mark that contradicts the room needs a reason or a corrected mark; lateness may be cleared
without one. Teachers and curators review their students' flags; a teacher's own flags are
for admins and heads. Same lesson, same fixtures as the record itself.
"""
from datetime import timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.meet_attendance import ReviewIn, list_lesson_records, restore_flag, review_flag
from src.schemas.models import Attendance, MeetFlagReview
from src.services import meet_presence
from tests.test_meet_presence import _codes, _record, _student, room  # noqa: F401 - fixture
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


def _review(room, user, who, code, **kw):
    return review_flag(room["lesson"].id, ReviewIn(user_id=who.id, code=code, **kw),
                       db=room["db"], current_user=user)


def _flag(record, who, code):
    return next(f for f in record["flags"] if f["user_id"] == who.id and f["code"] == code)


def _refused(status, call, *args, **kw):
    with pytest.raises(HTTPException) as err:
        call(*args, **kw)
    assert err.value.status_code == status, err.value.detail


@pytest.fixture
def never_joined(room):
    """Aya marked present, never in the room; the teacher there the whole lesson."""
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["mark"](room["aya"], "present")
    assert _record(room)["mismatches"] == 1
    return room


def test_a_mark_against_the_room_needs_a_reason(never_joined):
    room = never_joined
    _refused(422, _review, room, room["teacher"], room["aya"], "marked_present_not_joined")

    record = _review(room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="excused")
    flag = _flag(record, room["aya"], "marked_present_not_joined")
    assert flag["review"]["reason_label"] == "Отпросился"
    assert flag["review"]["by"] == "Гульзада Сапарова" and flag["review"]["at"].endswith("Z")
    assert record["mismatches"] == 0 and record["reviewed"] == 1
    assert "marked_present_not_joined" in _codes(_student(record, room["aya"])), "the flag stays, answered"


def test_other_needs_words_and_unknown_reasons_are_refused(never_joined):
    room = never_joined
    _refused(422, _review, room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="other")
    _refused(422, _review, room, room["teacher"], room["aya"], "marked_present_not_joined",
             reason_code="other", reason_text="   ")
    _refused(422, _review, room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="tech")

    record = _review(room, room["teacher"], room["aya"], "marked_present_not_joined",
                     reason_code="other", reason_text="  Сидела у брата, звонок не прошёл ")
    review = _flag(record, room["aya"], "marked_present_not_joined")["review"]
    assert (review["reason_label"], review["text"]) == ("Другое", "Сидела у брата, звонок не прошёл")


def test_lateness_can_be_cleared_without_a_reason(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (12, 60)), room["aya"])
    record = _review(room, room["teacher"], room["aya"], "late")
    review = _flag(record, room["aya"], "late")["review"]
    assert review["reason_code"] is None and review["reason_label"] is None
    assert record["reviewed"] == 1


def test_a_second_review_replaces_the_first_and_restore_brings_it_back(never_joined):
    room = never_joined
    _review(room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="excused")
    record = _review(room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="outside_meet")
    assert _flag(record, room["aya"], "marked_present_not_joined")["review"]["reason_label"] == "Занимался вне Meet"
    assert room["db"].query(MeetFlagReview).count() == 1

    record = restore_flag(room["lesson"].id, room["aya"].id, "marked_present_not_joined",
                          db=room["db"], current_user=room["teacher"])
    assert _flag(record, room["aya"], "marked_present_not_joined")["review"] is None
    assert record["mismatches"] == 1 and record["reviewed"] == 0


def test_only_a_flag_that_is_there_can_be_reviewed(never_joined):
    room = never_joined
    _refused(404, _review, room, room["teacher"], room["eldana"], "marked_present_not_joined", reason_code="excused")
    _refused(404, _review, room, room["teacher"], room["aya"], "late")


def test_a_lesson_not_finished_yet_has_nothing_to_review(room):
    upcoming = room["world"]["lesson"](room["group"], days_ahead=1)
    _refused(409, review_flag, upcoming.id, ReviewIn(user_id=room["aya"].id, code="late"),
             db=room["db"], current_user=room["teacher"])


# ── who reviews what ─────────────────────────────────────────────────────────────────────

def test_the_groups_curator_reviews_students_and_strangers_get_not_found(never_joined):
    room = never_joined
    db = room["db"]
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    record = _review(room, curator, room["aya"], "marked_present_not_joined", reason_code="other_device")
    assert _flag(record, room["aya"], "marked_present_not_joined")["review"]["by"] == curator.name
    for outsider in (_user(db, "curator"), _user(db, "teacher"), room["aya"]):
        _refused(404, _review, room, outsider, room["aya"], "marked_present_not_joined", reason_code="excused")


def test_a_teachers_own_flags_are_for_admins_and_heads(room):
    db = room["db"]
    room["link"](room["joined"]("Aya", (0, 60)), room["aya"])  # the teacher never came
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    for user in (room["teacher"], curator):
        _refused(403, _review, room, user, room["teacher"], "teacher_not_joined", reason_code="substitute")
    _refused(422, _review, room, _user(db, "head_teacher"), room["teacher"], "teacher_not_joined")

    record = _review(room, _user(db, "head_curator"), room["teacher"], "teacher_not_joined", reason_code="substitute")
    assert record["teacher"]["flags"][0]["review"]["reason_label"] == "Урок провёл другой преподаватель"
    assert record["mismatches"] == 0
    _refused(403, restore_flag, room["lesson"].id, room["teacher"].id, "teacher_not_joined",
             db=db, current_user=room["teacher"])


# ── correcting the mark instead ──────────────────────────────────────────────────────────

def _mark_of(room, who):
    return (room["db"].query(Attendance.status)
            .filter(Attendance.event_id == room["lesson"].id, Attendance.user_id == who.id).scalar())


def test_the_teacher_corrects_a_present_mark_to_absent(never_joined):
    room = never_joined
    record = _review(room, room["teacher"], room["aya"], "marked_present_not_joined", fix_mark=True)
    assert _mark_of(room, room["aya"]) == "absent"
    assert _codes(_student(record, room["aya"])) == {}, "the mark now agrees with the room"
    assert record["mismatches"] == 0 and room["db"].query(MeetFlagReview).count() == 0


def test_an_absent_mark_for_someone_in_the_room_becomes_present_or_late(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 60)), room["aya"])
    room["link"](room["joined"]("Eldana", (9, 60)), room["eldana"])
    room["mark"](room["aya"], "absent")
    room["mark"](room["eldana"], "absent")
    _review(room, room["teacher"], room["aya"], "marked_absent_was_in_room", fix_mark=True)
    record = _review(room, room["teacher"], room["eldana"], "marked_absent_was_in_room", fix_mark=True)
    assert (_mark_of(room, room["aya"]), _mark_of(room, room["eldana"])) == ("present", "late")
    assert "marked_absent_was_in_room" not in _codes(_student(record, room["eldana"]))


def test_curators_read_marks_but_never_correct_them(never_joined):
    room = never_joined
    db = room["db"]
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    _refused(403, _review, room, curator, room["aya"], "marked_present_not_joined", fix_mark=True)
    assert _mark_of(room, room["aya"]) == "present"
    _review(room, _user(db, "head_curator"), room["aya"], "marked_present_not_joined", fix_mark=True)
    assert _mark_of(room, room["aya"]) == "absent"


def test_only_marks_can_be_corrected(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (12, 60)), room["aya"])
    _refused(422, _review, room, room["teacher"], room["aya"], "late", fix_mark=True)


# ── where the reason is read ─────────────────────────────────────────────────────────────

def test_the_list_counts_open_and_reviewed_and_the_watch_page_shows_the_reason(never_joined):
    room = never_joined
    db = room["db"]
    _review(room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="excused")
    listing = list_lesson_records(date_from=None, date_to=None, teacher_id=None, group_id=None,
                                  db=db, current_user=_user(db, "admin"))
    assert listing["review_options"] == meet_presence.review_options(), "the list can open a review form"
    item = next(i for i in listing["items"] if i["event_id"] == room["lesson"].id)
    assert (item["mismatches"], item["reviewed"]) == (0, 1)
    assert _flag(item, room["aya"], "marked_present_not_joined")["review"]["reason_label"] == "Отпросился"

    view = meet_presence.public_participants(_record(room))
    aya = next(s for s in view["students"] if s["name"] == "Аяулым Сейтова")
    assert aya["flags"] == [{"code": "marked_present_not_joined", "minutes": None,
                             "review": {"reason_label": "Отпросился", "text": None}}]
    assert "by" not in repr(view["students"]), "who reviewed stays inside the LMS"


def test_every_flag_has_a_form_and_the_contradictions_require_a_reason():
    options = meet_presence.review_options()
    assert {code for code, o in options.items() if o["required"]} == meet_presence.REASON_REQUIRED
    assert all(o["reasons"][-1] == {"key": "other", "label": "Другое"} for o in options.values())
    assert [r["label"] for r in options["marked_present_not_joined"]["reasons"]][:3] == [
        "Отпросился", "С другого аккаунта или устройства", "Занимался вне Meet"]


def test_a_review_outlives_nothing_it_does_not_belong_to(never_joined):
    """Once the mark is corrected, an old review of that flag no longer shows or counts."""
    room = never_joined
    _review(room, room["teacher"], room["aya"], "marked_present_not_joined", reason_code="excused")
    room["db"].query(Attendance).filter(Attendance.user_id == room["aya"].id).update({"status": "absent"})
    room["db"].flush()
    record = _record(room, room["lesson"].end_datetime + timedelta(hours=2))
    assert record["reviewed"] == 0 and record["mismatches"] == 0
