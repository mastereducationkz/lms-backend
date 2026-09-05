"""scripts/checkpoint_pilot: readiness report, activation, enable/disable."""
import json

import pytest

from scripts.checkpoint_pilot import activate, disable, enable, group_report
from scripts.seed_sat_checkpoints import seed
from src.checkpoints import service
from src.checkpoints.models import CheckpointDefinition
from src.courses.models import Step
from tests.checkpoint_fixtures import make_user, make_group, enroll, make_sat_course, complete_lesson_explicit


@pytest.fixture
def db():
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
    course, v, m = make_sat_course(db, n_verbal=6, n_math=3)
    db.commit()
    seed(db, sat_course_id=course.id, blocks=3)
    group = make_group(db, enabled=False)
    students = [make_user(db, name=n) for n in ("Ahead", "Behind", "Middle")]
    for s in students:
        enroll(db, s, group, course, admin)
    blocks = [(v[0], v[1], m[0]), (v[2], v[3], m[1]), (v[4], v[5], m[2])]
    for block in blocks[:2]:                      # Ahead: blocks 1-2 done
        for l in block: complete_lesson_explicit(db, students[0], course, l)
    for l in blocks[0]:                           # Middle: block 1 done
        complete_lesson_explicit(db, students[2], course, l)
    db.flush()
    return admin, course, v, m, group, students


def test_report_suggests_the_block_most_of_the_group_is_on(db):
    _, course, v, m, group, students = _world(db)
    rep = group_report(db, course.id, group.id)
    by = {r["name"]: r for r in rep["students"]}
    assert by["Ahead"]["highest_block"] == 2 and by["Behind"]["highest_block"] == 0
    assert by["Middle"]["highest_block"] == 1
    assert rep["suggested_start"] == 2                          # median highest block is 1 -> start at 2
    assert by["Ahead"]["would_open_now"] == [2] and by["Middle"]["would_open_now"] == []
    assert rep["opens_immediately"] == 1 and rep["enabled"] is False


def test_report_uses_the_highest_block_not_the_contiguous_run(db):
    """A student who did blocks 1, 3 and 4 (block 2 has one unit never marked complete) is on
    block 5, not block 2 — a gap must not drag the suggestion back to the start."""
    _, course, v, m, group, students = _world(db)
    behind = students[1]
    for l in (v[0], v[1], m[0], v[4], v[5], m[2]):          # blocks 1 and 3, block 2 untouched
        complete_lesson_explicit(db, behind, course, l)
    db.flush()
    rep = group_report(db, course.id, group.id)
    by = {r["name"]: r for r in rep["students"]}
    assert by["Behind"]["complete_blocks"] == [1, 3] and by["Behind"]["highest_block"] == 3
    assert rep["suggested_start"] == 3                          # highest blocks 2, 3, 1 -> median 2 -> start 3
    assert by["Behind"]["would_open_now"] == [3] and by["Ahead"]["would_open_now"] == []


def test_activate_only_definitions_whose_quiz_is_full(db):
    _, course, v, m, group, students = _world(db)
    defs = db.query(CheckpointDefinition).filter_by(course_id=course.id).order_by(CheckpointDefinition.number).all()
    step = db.query(Step).filter(Step.lesson_id == defs[0].quiz_lesson_id).one()
    step.content_text = json.dumps({"questions": [{"id": f"q{i}"} for i in range(45)]}); db.flush()
    rows = activate(db, course.id, dry_run=True)
    assert [r["active"] for r in rows] == [True, False, False] and all(not d.is_active for d in defs)
    rows = activate(db, course.id)
    db.refresh(defs[0]); db.refresh(defs[1])
    assert defs[0].is_active is True and defs[1].is_active is False
    assert rows[0]["questions"] == 45 and rows[1]["questions"] == 0


def test_enable_runs_the_group_sync_with_staggered_deadlines(db):
    _, course, v, m, group, students = _world(db)
    for d in db.query(CheckpointDefinition).filter_by(course_id=course.id).all():
        d.is_active = True
    db.flush()
    assert enable(db, group.id, 1, dry_run=True)["opened"] is None and group.checkpoints_enabled is False
    out = enable(db, group.id, 1)
    db.refresh(group)
    assert group.checkpoints_enabled is True and group.checkpoints_start_number == 1
    assert out["opened"] == 3                                   # Ahead: 1 and 2, Middle: 1
    ahead = [service.get_row(db, students[0].id, group.id, d.id) for d in
             db.query(CheckpointDefinition).filter_by(course_id=course.id).order_by(CheckpointDefinition.number).all()[:2]]
    assert (ahead[1].deadline - ahead[0].deadline).total_seconds() == 24 * 3600
    assert disable(db, group.id)["enabled"] is False
    db.refresh(group)
    assert group.checkpoints_enabled is False
