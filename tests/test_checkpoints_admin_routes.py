import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from src.checkpoints.schemas import DefinitionUpdate, RequiredUnitInput, GroupSettingsUpdate, OpenRequest, DeadlineUpdate
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()
    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close(); trans.rollback(); connection.close()


def _world(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    quiz_course, quiz_lesson, quiz_step = make_quiz_lesson(db, n_questions=2)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lesson)
    group = make_group(db, enabled=False)
    s1, s2 = make_user(db, name="Alice"), make_user(db, name="Bob")
    enroll(db, s1, group, course, admin); enroll(db, s2, group, course, admin)
    return admin, course, v, m, quiz_step, d1, group, s1, s2


def test_definitions_list_and_update(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)
    out = r.list_definitions(course_id=course.id, current_user=admin, db=db)
    assert len(out) == 1 and out[0]["number"] == 1 and out[0]["question_count"] == 2
    assert [u["kind"] for u in out[0]["required_units"]] == ["verbal", "verbal", "math"]
    upd = r.update_definition(d1.id, DefinitionUpdate(
        is_active=False, required_units=[RequiredUnitInput(lesson_id=v[2].id, kind="verbal"),
                                         RequiredUnitInput(lesson_id=v[3].id, kind="verbal"),
                                         RequiredUnitInput(lesson_id=m[1].id, kind="math")]),
        current_user=admin, db=db)
    assert upd["is_active"] is False and [u["lesson_id"] for u in upd["required_units"]] == [v[2].id, v[3].id, m[1].id]
    with pytest.raises(HTTPException) as e:   # 2 verbal + 1 math is enforced
        r.update_definition(d1.id, DefinitionUpdate(required_units=[RequiredUnitInput(lesson_id=v[0].id, kind="verbal")]),
                            current_user=admin, db=db)
    assert e.value.status_code == 400


def test_update_required_units_partial_overlap(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)          # d1 requires v[0], v[1], m[0]
    upd = r.update_definition(d1.id, DefinitionUpdate(required_units=[
        RequiredUnitInput(lesson_id=v[0].id, kind="verbal"),
        RequiredUnitInput(lesson_id=v[2].id, kind="verbal"),
        RequiredUnitInput(lesson_id=m[0].id, kind="math")]), current_user=admin, db=db)
    assert [u["lesson_id"] for u in upd["required_units"]] == [v[0].id, v[2].id, m[0].id]
    from src.checkpoints.models import CheckpointRequiredUnit
    assert db.query(CheckpointRequiredUnit).filter_by(checkpoint_id=d1.id).count() == 3


def test_quiz_check_reports_composition(db):
    from src.checkpoints.routes import checkpoints as r
    admin, _, _, _, _, d1, _, _, _ = _world(db)
    out = r.quiz_check(d1.id, current_user=admin, db=db)
    assert out["question_count"] == 2 and out["expected"] == 45
    assert out["by_difficulty"] == {"easy": 2, "medium": 0, "hard": 0, "unset": 0}
    assert any("45" in p for p in out["problems"])


def test_group_settings_enable_opens_eligible(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    for l in (v[0], v[1], m[0]):
        complete_lesson_explicit(db, s1, course, l)
    out = r.update_group_settings(group.id, GroupSettingsUpdate(enabled=True), current_user=admin, db=db)
    assert out["checkpoints_enabled"] is True and out["opened"] == 1
    groups = r.list_groups(program_type="sat", current_user=admin, db=db)
    assert any(g["id"] == group.id and g["checkpoints_enabled"] for g in groups)


def test_matrix_open_reopen_deadline_results(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    r.update_group_settings(group.id, GroupSettingsUpdate(enabled=True), current_user=admin, db=db)
    complete_lesson_explicit(db, s1, course, v[0])
    mat = r.group_matrix(group.id, current_user=admin, db=db)
    assert [d["number"] for d in mat["definitions"]] == [1]
    alice = next(s for s in mat["students"] if s["name"] == "Alice")
    cell = alice["cells"][0]
    assert cell["status"] == "locked" and cell["locked_reason"].startswith("Locked — waiting for")
    assert [u["completed"] for u in cell["units"]] == [True, False, False]

    opened = r.open_checkpoint(group.id, d1.id, OpenRequest(), current_user=admin, db=db)
    assert opened["changed"] == 2
    re = r.reopen_checkpoint(group.id, d1.id, OpenRequest(student_ids=[s2.id]), current_user=admin, db=db)
    assert re["changed"] == 1 and re["rows"][0]["status"] == "reopened"

    row_id = opened["rows"][0]["id"]
    new_dl = datetime(2030, 1, 1, 12, 0)
    upd = r.update_deadline(row_id, DeadlineUpdate(deadline=new_dl), current_user=admin, db=db)
    assert upd["deadline"] == "2030-01-01T12:00:00Z"

    res = r.checkpoint_results(group.id, d1.id, current_user=admin, db=db)
    assert {x["name"] for x in res} == {"Alice", "Bob"}


def test_matrix_two_students_two_definitions_batched(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)          # d1 requires v[0], v[1], m[0]
    d2 = make_definition(db, course, 2, v[2:4], m[1])                # d2 requires v[2], v[3], m[1]

    complete_lesson_explicit(db, s1, course, v[0])
    complete_lesson_explicit(db, s1, course, v[1])
    complete_lesson_explicit(db, s1, course, m[0])
    complete_lesson_explicit(db, s2, course, v[2])

    mat = r.group_matrix(group.id, current_user=admin, db=db)
    assert [d["number"] for d in mat["definitions"]] == [1, 2]

    alice = next(s for s in mat["students"] if s["name"] == "Alice")
    bob = next(s for s in mat["students"] if s["name"] == "Bob")

    a_cell1, a_cell2 = alice["cells"]
    assert [u["completed"] for u in a_cell1["units"]] == [True, True, True]
    assert a_cell1["locked_reason"] is None
    assert [u["completed"] for u in a_cell2["units"]] == [False, False, False]
    assert a_cell2["locked_reason"].startswith("Locked — waiting for")

    b_cell1, b_cell2 = bob["cells"]
    assert [u["completed"] for u in b_cell1["units"]] == [False, False, False]
    assert b_cell1["locked_reason"].startswith("Locked — waiting for")
    assert [u["completed"] for u in b_cell2["units"]] == [True, False, False]
    assert b_cell2["locked_reason"].startswith("Locked — waiting for")


def test_non_admin_cannot_write_but_group_teacher_can_read(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    teacher = make_user(db, role="teacher"); group.teacher_id = teacher.id; db.flush()
    with pytest.raises(HTTPException) as e:
        r.update_group_settings(group.id, GroupSettingsUpdate(enabled=True), current_user=teacher, db=db)
    assert e.value.status_code == 403
    assert r.group_matrix(group.id, current_user=teacher, db=db)["group"]["id"] == group.id
    outsider = make_user(db, role="teacher")
    with pytest.raises(HTTPException) as e:
        r.group_matrix(group.id, current_user=outsider, db=db)
    assert e.value.status_code == 403
