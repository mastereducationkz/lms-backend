import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.checkpoints.schemas import DefinitionUpdate, RequiredUnitInput, GroupSettingsUpdate, OpenRequest, DeadlineUpdate
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_quiz_lesson, make_definition, complete_lesson_explicit,
)


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its savepoint by
    # reacting to the transaction the app's own db.commit() just tore down, so an app-level
    # db.rollback() straight after a commit unwinds past it and takes committed rows with it.
    # create_savepoint keeps every app-level commit/rollback nested one level down, inside a
    # connection-level transaction this fixture always rolls back. See tests/onboarding_fixtures.py.
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
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


def test_group_teacher_and_curator_manage_their_own_group_only(db):
    """Every staff role works with checkpoints (2026-09-05): teachers and curators read and act on
    the groups they own, head roles on every group; outsiders and students get nothing."""
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    teacher = make_user(db, role="teacher"); group.teacher_id = teacher.id
    curator = make_user(db, role="curator"); group.curator_id = curator.id
    db.flush()
    assert r.update_group_settings(group.id, GroupSettingsUpdate(enabled=True), current_user=teacher, db=db)["checkpoints_enabled"]
    assert r.group_matrix(group.id, current_user=curator, db=db)["group"]["id"] == group.id
    opened = r.open_checkpoint(group.id, d1.id, OpenRequest(student_ids=[s1.id]), current_user=curator, db=db)
    assert opened["changed"] == 1
    row_id = opened["rows"][0]["id"]
    new_deadline = datetime.now(timezone.utc) + timedelta(days=5)
    assert r.update_deadline(row_id, DeadlineUpdate(deadline=new_deadline), current_user=teacher, db=db)["deadline"]
    assert r.reopen_checkpoint(group.id, d1.id, OpenRequest(student_ids=[s1.id]), current_user=teacher, db=db)["changed"] == 1
    assert len(r.checkpoint_results(group.id, d1.id, current_user=curator, db=db)) == 2
    assert [g["id"] for g in r.list_groups(program_type="sat", current_user=curator, db=db)] == [group.id]

    for role in ("head_curator", "head_teacher"):
        head = make_user(db, role=role)
        assert r.group_matrix(group.id, current_user=head, db=db)["group"]["id"] == group.id
        assert r.update_group_settings(group.id, GroupSettingsUpdate(start_number=2), current_user=head, db=db)["checkpoints_start_number"] == 2

    outsider = make_user(db, role="teacher")
    for call in (lambda: r.group_matrix(group.id, current_user=outsider, db=db),
                 lambda: r.update_group_settings(group.id, GroupSettingsUpdate(enabled=False), current_user=outsider, db=db),
                 lambda: r.open_checkpoint(group.id, d1.id, OpenRequest(), current_user=outsider, db=db),
                 lambda: r.update_deadline(row_id, DeadlineUpdate(deadline=new_deadline), current_user=outsider, db=db),
                 lambda: r.checkpoint_results(group.id, d1.id, current_user=outsider, db=db),
                 lambda: r.group_matrix(group.id, current_user=s1, db=db),
                 lambda: r.list_definitions(course_id=None, current_user=s1, db=db)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403


def test_definitions_are_written_by_admins_and_head_roles_only(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    teacher = make_user(db, role="teacher"); group.teacher_id = teacher.id
    curator = make_user(db, role="curator"); group.curator_id = curator.id
    db.flush()
    for who in (teacher, curator):
        assert r.list_definitions(course_id=course.id, current_user=who, db=db)[0]["id"] == d1.id
        assert "by_difficulty" in r.quiz_check(d1.id, current_user=who, db=db)
        with pytest.raises(HTTPException) as e:
            r.update_definition(d1.id, DefinitionUpdate(title="x"), current_user=who, db=db)
        assert e.value.status_code == 403
    for role in ("head_curator", "head_teacher"):
        assert r.update_definition(d1.id, DefinitionUpdate(title=f"by {role}"), current_user=make_user(db, role=role), db=db)["title"] == f"by {role}"


def test_required_units_allow_a_double_unit_lesson(db):
    """The IT mapping binds some checkpoints to 4 units (a course lesson holding two LMS units):
    3 or 4 units, at least 2 verbal and 1 math, all distinct."""
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)
    ok = r.update_definition(d1.id, DefinitionUpdate(required_units=[
        RequiredUnitInput(lesson_id=v[0].id, kind="verbal"), RequiredUnitInput(lesson_id=v[1].id, kind="verbal"),
        RequiredUnitInput(lesson_id=m[0].id, kind="math"), RequiredUnitInput(lesson_id=m[1].id, kind="math")]),
        current_user=admin, db=db)
    assert [u["kind"] for u in ok["required_units"]] == ["verbal", "verbal", "math", "math"]
    for units in (
        [RequiredUnitInput(lesson_id=v[0].id, kind="verbal"), RequiredUnitInput(lesson_id=m[0].id, kind="math")],       # 2
        [RequiredUnitInput(lesson_id=v[0].id, kind="verbal"), RequiredUnitInput(lesson_id=v[1].id, kind="verbal"),
         RequiredUnitInput(lesson_id=v[2].id, kind="verbal")],                                                        # no math
        [RequiredUnitInput(lesson_id=v[0].id, kind="verbal"), RequiredUnitInput(lesson_id=v[1].id, kind="verbal"),
         RequiredUnitInput(lesson_id=v[2].id, kind="verbal"), RequiredUnitInput(lesson_id=m[0].id, kind="math"),
         RequiredUnitInput(lesson_id=m[1].id, kind="math")],                                                          # 5
    ):
        with pytest.raises(HTTPException) as e:
            r.update_definition(d1.id, DefinitionUpdate(required_units=units), current_user=admin, db=db)
        assert e.value.status_code == 400


def test_quiz_lesson_cannot_be_shared_by_two_definitions(db):
    """Two definitions on one quiz lesson would make checkpoint_definition_for_step ambiguous."""
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)
    d2 = make_definition(db, course, 2, v[2:4], m[1])          # no quiz lesson yet
    with pytest.raises(HTTPException) as e:
        r.update_definition(d2.id, DefinitionUpdate(quiz_lesson_id=d1.quiz_lesson_id),
                            current_user=admin, db=db)
    assert e.value.status_code == 400
    # re-pointing a definition at its own quiz lesson is still fine
    assert r.update_definition(d1.id, DefinitionUpdate(quiz_lesson_id=d1.quiz_lesson_id),
                               current_user=admin, db=db)["quiz_lesson_id"] == d1.quiz_lesson_id


def test_cannot_activate_a_definition_whose_quiz_is_short(db):
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)           # quiz has 2 questions, expects 45
    r.update_definition(d1.id, DefinitionUpdate(is_active=False), current_user=admin, db=db)
    with pytest.raises(HTTPException) as e:
        r.update_definition(d1.id, DefinitionUpdate(is_active=True), current_user=admin, db=db)
    assert e.value.status_code == 400 and "45" in e.value.detail
    # matching the expected count in the same call is allowed
    upd = r.update_definition(d1.id, DefinitionUpdate(is_active=True, total_questions=2),
                              current_user=admin, db=db)
    assert upd["is_active"] is True and upd["question_count"] == 2


def test_list_groups_counts_students_in_one_query(db):
    from sqlalchemy import event
    from src.config import engine
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, group, s1, s2 = _world(db)
    other = make_group(db, name="cp-grp-2")
    enroll(db, make_user(db), other, course, admin)
    empty = make_group(db, name="cp-grp-3")
    db.commit()

    seen = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement.split("\n")[0])

    try:
        out = r.list_groups(program_type="sat", current_user=admin, db=db)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    counts = {g["id"]: g["student_count"] for g in out}
    assert counts[group.id] == 2 and counts[other.id] == 1 and counts[empty.id] == 0
    assert len(seen) <= 4, f"{len(seen)} statements:\n" + "\n".join(seen)


def test_active_definition_cannot_be_edited_into_a_question_mismatch(db):
    """The guard used to read `body.is_active`, so it never fired on a definition that was
    ALREADY active — an admin could retune total_questions (or repoint the quiz) on a live
    checkpoint and publish a mismatch to every enabled group."""
    from src.checkpoints.routes import checkpoints as r
    from tests.checkpoint_fixtures import make_quiz_lesson
    admin, course, v, m, _, d1, _, _, _ = _world(db)           # active, linked quiz has 2 questions
    assert r.update_definition(d1.id, DefinitionUpdate(total_questions=2),
                               current_user=admin, db=db)["total_questions"] == 2
    with pytest.raises(HTTPException) as e:
        r.update_definition(d1.id, DefinitionUpdate(total_questions=45), current_user=admin, db=db)
    assert e.value.status_code == 400 and "45" in e.value.detail
    # repointing a live checkpoint at a quiz of the wrong size is refused the same way
    _, other_lesson, _ = make_quiz_lesson(db, title="Checkpoint 9", n_questions=5)
    with pytest.raises(HTTPException) as e:
        r.update_definition(d1.id, DefinitionUpdate(quiz_lesson_id=other_lesson.id),
                            current_user=admin, db=db)
    assert e.value.status_code == 400
    # ...but the row itself is untouched by the rejected requests
    db.refresh(d1)
    assert d1.total_questions == 2 and d1.quiz_lesson_id != other_lesson.id


def test_editing_an_active_definition_without_touching_the_quiz_is_allowed(db):
    """A title or required-units edit must not be blocked by a pre-existing mismatch."""
    from src.checkpoints.routes import checkpoints as r
    admin, course, v, m, _, d1, _, _, _ = _world(db)           # active, expects 45, quiz has 2
    out = r.update_definition(d1.id, DefinitionUpdate(title="Checkpoint one"), current_user=admin, db=db)
    assert out["title"] == "Checkpoint one" and out["is_active"] is True
