"""Meet's verdict — what the register would say if Meet took it (owner, 2026-09-16).

Late after more than 5 whole minutes, absent under 75% of the lesson actually held, the clock
following the teacher, every rounding in the student's favour. Shown beside the teacher's mark and
applied only by a person. Times are minutes from the lesson's scheduled start.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.events.routes.meet_attendance import ApplyVerdictsIn, ReviewIn, apply_verdicts, list_lesson_records, review_flag
from src.schemas.models import Attendance
from src.services import meet_presence as mp
from tests.test_meet_presence import _codes, _record, _student, room  # noqa: F401 - fixture
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures

T0 = datetime(2026, 9, 16, 13, 0)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def spans(*pairs) -> list:
    return [(at(a), at(b)) for a, b in pairs]


def clock(teacher=((0, 60),), length=60) -> dict:
    return mp.lesson_clock(T0, at(length), spans(*teacher))


def judge(*student, teacher=((0, 60),), length=60, unknown_in_room=False) -> dict:
    return mp.verdict(clock(teacher, length), spans(*student), unknown_in_room=unknown_in_room)


# ── the rules ────────────────────────────────────────────────────────────────────────────

def test_the_whole_lesson_is_present():
    v = judge((-3, 61))
    assert (v["verdict"], v["attended"], v["minutes"], v["required"], v["late_minutes"]) == ("present", True, 60, 45, 0)


def test_late_counts_whole_minutes_so_five_fifty_nine_is_on_time():
    assert judge((5 + 59 / 60, 60))["verdict"] == "present"
    v = judge((6, 60))
    assert (v["verdict"], v["late_minutes"]) == ("late", 6)


def test_the_time_needed_rounds_up_for_the_student():
    assert judge((0, 44))["verdict"] == "absent"
    assert judge((0, 44 + 1 / 60))["verdict"] == "present", "44:01 of 60 is the 45 needed"


def test_absent_beats_late():
    v = judge((20, 60))
    assert (v["verdict"], v["minutes"], v["late_minutes"]) == ("absent", 40, 20)


@pytest.mark.parametrize("length, needed", [(60, 45), (90, 67), (120, 90), (40, 30)])
def test_three_quarters_of_any_length(length, needed):
    c = clock(teacher=((0, length),), length=length)
    assert (c["held_minutes"], c["required_minutes"]) == (length, needed)
    assert judge((0, needed), teacher=((0, length),), length=length)["verdict"] == "present"
    assert judge((0, needed - 1), teacher=((0, length),), length=length)["verdict"] == "absent"


def test_never_in_the_lesson_is_absent():
    v = judge()
    assert (v["verdict"], v["attended"], v["minutes"]) == ("absent", False, 0)


def test_gaps_are_left_out_and_two_devices_count_once():
    assert judge((0, 20), (30, 55))["minutes"] == 45
    assert judge((0, 50), (10, 40))["minutes"] == 50


def test_a_room_check_before_the_lesson_is_not_the_arrival():
    """First join used to be any visit in the 30 minutes before: a 10-second check made anyone on time."""
    v = judge((-25, -24.8), (15, 60))
    assert (v["verdict"], v["late_minutes"]) == ("late", 15)
    # In the room when the lesson began, dropped, back later: on time — and the minutes decide.
    assert judge((-2, 1), (12, 60))["verdict"] == "present"


# ── the clock follows the teacher ────────────────────────────────────────────────────────

def test_a_late_teacher_moves_the_start_and_shrinks_the_time_needed():
    c = clock(teacher=((10, 60),))
    assert (c["start"], c["end"], c["held_minutes"], c["required_minutes"], c["follows_teacher"]) == (
        at(10), at(60), 50, 37, True)
    assert judge((9, 60), teacher=((10, 60),))["verdict"] == "present", "came with the teacher"
    assert judge((15, 60), teacher=((10, 60),))["verdict"] == "present"
    assert judge((16, 60), teacher=((10, 60),))["verdict"] == "late"


def test_a_teacher_who_ended_early_ends_the_lesson():
    c = clock(teacher=((0, 40),))
    assert (c["end"], c["count_until"], c["required_minutes"]) == (at(40), at(40), 30)
    assert judge((0, 40), teacher=((0, 40),))["verdict"] == "present"
    assert judge((0, 29), teacher=((0, 40),))["verdict"] == "absent"


def test_overrun_minutes_count_for_students_but_never_raise_the_bar():
    c = clock(teacher=((0, 80),))
    assert (c["end"], c["count_until"], c["required_minutes"]) == (at(60), at(80), 45)
    assert judge((30, 80), teacher=((0, 80),))["verdict"] == "late", "50 minutes, 30 of them late"
    assert clock(teacher=((0, 200),))["count_until"] == at(90), "no further than the lesson margin"


def test_without_the_teacher_in_the_lesson_the_timetable_decides():
    for teacher in ((), ((-20, -10),), ((0, 3),)):
        c = clock(teacher=teacher)
        assert (c["start"], c["end"], c["follows_teacher"]) == (T0, at(60), False), teacher


# ── an account nobody has confirmed may be the student ───────────────────────────────────

def test_an_unconfirmed_account_holds_back_absent_and_late_but_not_present():
    absent = judge((0, 20), unknown_in_room=True)
    assert (absent["verdict"], absent["held_back"], absent["attended"]) == (None, True, None)
    late = judge((12, 60), unknown_in_room=True)
    assert (late["verdict"], late["held_back"], late["attended"]) == (None, True, True), "attended either way"
    present = judge((0, 60), unknown_in_room=True)
    assert (present["verdict"], present["held_back"]) == ("present", False)


def test_when_even_the_teacher_is_not_known_nothing_is_judged():
    v = judge((0, 60), teacher=(), unknown_in_room=True)
    assert (v["verdict"], v["held_back"], v["attended"]) == (None, True, None), "that account may be a late teacher"


# ── the record: marks against the verdict ────────────────────────────────────────────────

def test_marked_present_but_in_the_lesson_under_three_quarters(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 30)), room["aya"])
    room["mark"](room["aya"], "late")
    record = _record(room)
    aya = _student(record, room["aya"])
    assert aya["verdict"]["verdict"] == "absent"
    assert _codes(aya) == {"marked_present_too_short": 30, "left_early": 30}
    flag = next(f for f in aya["flags"] if f["code"] == "marked_present_too_short")
    assert flag["required"] == 45
    assert record["mismatches"] == 1


def test_an_absent_mark_meet_agrees_with_is_not_flagged_any_more(room):
    """Was «in the room 10 minutes or more»; under the rules 30 of 45 minutes is absent too."""
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 30)), room["aya"])
    room["link"](room["joined"]("Eldana", (0, 50)), room["eldana"])
    room["mark"](room["aya"], "absent")
    room["mark"](room["eldana"], "absent")
    record = _record(room)
    assert "marked_absent_was_in_room" not in _codes(_student(record, room["aya"]))
    assert _codes(_student(record, room["eldana"]))["marked_absent_was_in_room"] == 50


def test_a_late_student_marked_present_is_a_note_not_a_disagreement(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (12, 60)), room["aya"])
    room["mark"](room["aya"], "present")
    record = _record(room)
    aya = _student(record, room["aya"])
    assert aya["verdict"]["verdict"] == "late" and _codes(aya) == {"late": 12}
    assert record["mismatches"] == 0


def test_the_summary_counts_verdicts_agreement_and_what_can_be_applied(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 60)), room["aya"])
    room["link"](room["joined"]("Eldana", (8, 60)), room["eldana"])
    room["mark"](room["aya"], "present")      # agrees
    room["mark"](room["eldana"], "absent")    # disagrees: 52 minutes, late
    # Шыңғыс: not marked, never came
    record = _record(room)
    assert record["verdict_summary"] == {"present": 1, "late": 1, "absent": 1, "held_back": 0,
                                         "unmarked": 1, "applicable": 1, "compared": 2, "agree": 1}
    assert record["clock"]["required_minutes"] == 45 and record["clock"]["follows_teacher"] is True
    assert record["rules"] == {"late_after_minutes": 5, "present_share": 0.75}


def test_the_list_carries_each_students_verdict_for_the_journal(room):
    db = room["db"]
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (7, 60)), room["aya"])
    listing = list_lesson_records(date_from=None, date_to=None, teacher_id=None, group_id=room["group"].id,
                                  db=db, current_user=_user(db, "admin"))
    assert listing["verdict_rules"] == {"late_after_minutes": 5, "present_share": 0.75}
    item = next(i for i in listing["items"] if i["event_id"] == room["lesson"].id)
    aya = next(v for v in item["verdicts"] if v["user_id"] == room["aya"].id)
    assert aya == {"user_id": room["aya"].id, "verdict": "late", "held_back": False,
                   "minutes": 53, "required": 45, "late_minutes": 7}
    assert item["verdict_summary"]["unmarked"] == 3


def test_the_watch_page_gets_no_verdicts(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 30)), room["aya"])
    room["mark"](room["aya"], "present")
    view = mp.public_participants(_record(room))
    assert "verdict" not in repr(view) and "clock" not in view
    aya = next(s for s in view["students"] if s["name"] == "Аяулым Сейтова")
    assert [f["code"] for f in aya["flags"]] == ["left_early"], "the 75% rule stays inside the LMS"


# ── answering the new disagreement ───────────────────────────────────────────────────────

def _too_short(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 30)), room["aya"])
    room["mark"](room["aya"], "present")
    return room


def test_too_short_needs_a_reason_or_becomes_absent(room):
    _too_short(room)
    with pytest.raises(HTTPException) as err:
        review_flag(room["lesson"].id, ReviewIn(user_id=room["aya"].id, code="marked_present_too_short"),
                    db=room["db"], current_user=room["teacher"])
    assert err.value.status_code == 422
    record = review_flag(room["lesson"].id, ReviewIn(user_id=room["aya"].id, code="marked_present_too_short",
                                                     reason_code="connection"),
                         db=room["db"], current_user=room["teacher"])
    flag = next(f for f in _student(record, room["aya"])["flags"] if f["code"] == "marked_present_too_short")
    assert flag["review"]["reason_label"] == "Проблемы со связью" and record["mismatches"] == 0

    record = review_flag(room["lesson"].id, ReviewIn(user_id=room["aya"].id, code="marked_present_too_short",
                                                     fix_mark=True),
                         db=room["db"], current_user=room["teacher"])
    assert _student(record, room["aya"])["mark"] == "absent"
    assert "marked_present_too_short" not in _codes(_student(record, room["aya"]))


def test_the_new_reasons_are_offered():
    options = mp.review_options()["marked_present_too_short"]
    assert options["required"] is True
    assert [r["label"] for r in options["reasons"]] == [
        "Отпросился раньше", "Проблемы со связью", "С другого аккаунта или устройства", "Другое"]


# ── applying Meet's verdicts to students nobody has marked ───────────────────────────────

def _statuses(room) -> dict:
    return dict(room["db"].query(Attendance.user_id, Attendance.status)
                .filter(Attendance.event_id == room["lesson"].id))


def _apply(room, user, **kw):
    return apply_verdicts(room["lesson"].id, ApplyVerdictsIn(**kw), db=room["db"], current_user=user)


def _unmarked_class(room):
    room["link"](room["joined"]("Gulzada", (0, 60)), room["teacher"])
    room["link"](room["joined"]("Aya", (0, 60)), room["aya"])
    room["link"](room["joined"]("Eldana", (9, 60)), room["eldana"])
    return room


def test_apply_fills_only_the_unmarked(room):
    _unmarked_class(room)
    room["mark"](room["aya"], "absent")  # the teacher's mark is never overwritten here
    record = _apply(room, room["teacher"])
    assert _statuses(room) == {room["aya"].id: "absent", room["eldana"].id: "late", room["shyngys"].id: "absent"}
    assert record["verdict_summary"]["applicable"] == 0
    scores = dict(room["db"].query(Attendance.user_id, Attendance.score)
                  .filter(Attendance.event_id == room["lesson"].id))
    assert (scores[room["eldana"].id], scores[room["shyngys"].id]) == (1, 0)


def test_apply_can_be_limited_to_some_students(room):
    _unmarked_class(room)
    _apply(room, room["teacher"], user_ids=[room["eldana"].id])
    assert _statuses(room) == {room["eldana"].id: "late"}


def test_apply_skips_what_is_held_back(room):
    _unmarked_class(room)
    room["joined"]("iPhone 13", (1, 60))
    _apply(room, room["teacher"])
    assert _statuses(room) == {room["aya"].id: "present"}, "Eldana's lateness and Шыңғыс's absence wait for the iPhone"


def test_apply_follows_the_journals_marking_rights(room):
    db = _unmarked_class(room)["db"]
    curator = _user(db, "curator")
    room["group"].curator_id = curator.id
    db.flush()
    with pytest.raises(HTTPException) as err:
        _apply(room, curator)
    assert err.value.status_code == 403
    with pytest.raises(HTTPException) as err:
        _apply(room, _user(db, "teacher"))
    assert err.value.status_code == 404, "another teacher's lesson is not theirs to see"
    assert _statuses(room) == {}
    _apply(room, _user(db, "head_curator"))
    assert len(_statuses(room)) == 3


def test_apply_waits_for_a_finished_record(room):
    _unmarked_class(room)
    upcoming = room["world"]["lesson"](room["group"], days_ahead=1)
    with pytest.raises(HTTPException) as err:
        apply_verdicts(upcoming.id, ApplyVerdictsIn(), db=room["db"], current_user=room["teacher"])
    assert err.value.status_code == 409
