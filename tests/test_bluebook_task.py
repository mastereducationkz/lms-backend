"""Bluebook homework task: content validation and the queryable projection.

Mirrors the style of tests/test_audio_task_multitask.py - the validation half is pure
and runs without Postgres, which matters because a third of this suite skips silently
when no database is reachable.
"""
from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException

from src.schemas.models import Assignment, AssignmentSubmission, Group, GroupStudent, UserInDB  # noqa: F401  (import-order guard)
from src.assignments.routes.assignments import validate_assignment_content
from src.exams.projection import project_bluebook_answers


def validate_multi_task_content(content):
    """Thin alias: these tests only ever validate multi_task assignments."""
    return validate_assignment_content("multi_task", content)


def _task(test_number=7, task_id="task_1"):
    return {
        "id": task_id,
        "task_type": "bluebook_task",
        "title": "Bluebook Test #7",
        "order_index": 0,
        "points": 10,
        "content": {"test_number": test_number},
    }


def _content(*tasks):
    return {"tasks": list(tasks)}


# --------------------------------------------------------------------------------------
# Teacher-side content validation (no DB)
# --------------------------------------------------------------------------------------

def test_valid_bluebook_task_is_accepted():
    validate_multi_task_content(_content(_task(7)))


@pytest.mark.parametrize("n", [4, 5, 6, 7, 8, 9, 10, 11])
def test_every_allowed_test_number_is_accepted(n):
    validate_multi_task_content(_content(_task(n)))


@pytest.mark.parametrize("n", [3, 12, 0, -1, 99])
def test_test_number_outside_4_to_11_is_rejected(n):
    """The UI selector is a convenience, not a security boundary."""
    with pytest.raises(HTTPException) as exc:
        validate_multi_task_content(_content(_task(n)))
    assert exc.value.status_code == 400
    assert "test_number" in str(exc.value.detail)


@pytest.mark.parametrize("bad", ["7", 7.5, None, True, [], {}])
def test_non_integer_test_number_is_rejected(bad):
    with pytest.raises(HTTPException) as exc:
        validate_multi_task_content(_content(_task(bad)))
    assert exc.value.status_code == 400


def test_missing_test_number_is_rejected():
    task = _task()
    task["content"] = {}
    with pytest.raises(HTTPException) as exc:
        validate_multi_task_content(_content(task))
    assert exc.value.status_code == 400


def test_bluebook_task_composes_with_other_task_types():
    """The reason for putting Bluebook inside multi_task: one homework can mix
    'watch these lessons' with 'submit Bluebook #7'."""
    text_task = {
        "id": "task_2", "task_type": "text_task", "title": "Reflection",
        "order_index": 1, "points": 5, "content": {"question": "How did it go?"},
    }
    validate_multi_task_content(_content(_task(7), text_task))


def test_bluebook_task_is_not_bulk_auto_gradable():
    """A self-reported score with a screenshot needs a human to look at it."""
    from src.admin.routes.dashboard import _NEEDS_REVIEW_TASK_TYPES
    assert "bluebook_task" in _NEEDS_REVIEW_TASK_TYPES


# --------------------------------------------------------------------------------------
# Projection (needs Postgres)
# --------------------------------------------------------------------------------------

@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def scenario(db):
    student = UserInDB(email="bb-stu@t.io", name="BB Student", hashed_password="x",
                       role="student", is_active=True)
    db.add(student)
    db.flush()

    group = Group(name="bb Group", is_active=True, is_over=False, program_type="sat")
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))

    import json
    assignment = Assignment(
        title="Bluebook #7", description="", assignment_type="multi_task",
        content=json.dumps(_content(_task(7))), max_score=10,
        group_id=group.id, due_date=datetime(2026, 7, 6, 12, 0),
        is_active=True,
    )
    db.add(assignment)
    db.flush()

    submission = AssignmentSubmission(
        assignment_id=assignment.id, user_id=student.id,
        answers="{}", max_score=10, is_graded=False,
        submitted_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(submission)
    db.flush()

    import json as _json
    return dict(student=student, group=group, assignment=assignment,
                submission=submission,
                content=_json.loads(assignment.content))


def _project(db, scenario, answer):
    return project_bluebook_answers(
        db,
        assignment=scenario["assignment"],
        submission=scenario["submission"],
        answers={"task_1": answer},
        assignment_content=scenario["content"],
    )


def test_projection_writes_a_row_with_derived_total(db, scenario):
    from src.exams.models import BluebookResult
    assert _project(db, scenario, {"verbal_score": 640, "math_score": 780}) == 1
    db.flush()

    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.verbal_score == 640
    assert row.math_score == 780
    assert row.total_score == 1420          # matches the reference sheet
    assert row.group_id == scenario["group"].id
    assert row.source == "homework"


def test_projection_ignores_a_client_supplied_total(db, scenario):
    from src.exams.models import BluebookResult
    _project(db, scenario, {"verbal_score": 600, "math_score": 600, "total_score": 9999})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.total_score == 1200


def test_projection_dates_the_row_from_the_assignment_due_date(db, scenario):
    from src.exams.models import BluebookResult
    _project(db, scenario, {"verbal_score": 600, "math_score": 600})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.taken_at == date(2026, 7, 6)


def test_projection_prefers_an_explicit_taken_at(db, scenario):
    from src.exams.models import BluebookResult
    _project(db, scenario, {"verbal_score": 600, "math_score": 600, "taken_at": "2026-07-01"})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.taken_at == date(2026, 7, 1)


def test_projection_captures_the_screenshot_url(db, scenario):
    from src.exams.models import BluebookResult
    _project(db, scenario, {
        "verbal_score": 600, "math_score": 600,
        "files": [{"file_url": "/uploads/x/shot.png", "file_name": "shot.png"}],
    })
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.screenshot_url == "/uploads/x/shot.png"


@pytest.mark.parametrize("answer", [
    {"verbal_score": 640},                              # missing math
    {"math_score": 780},                                # missing verbal
    {"verbal_score": 0, "math_score": 780},             # below range
    {"verbal_score": 640, "math_score": 900},           # above range
    {"verbal_score": 645, "math_score": 780},           # not a multiple of 10
    {"verbal_score": "abc", "math_score": "def"},       # non-numeric
    {},                                                  # empty
])
def test_projection_skips_invalid_or_incomplete_answers(db, scenario, answer):
    """A half-row would silently distort the group's averages."""
    from src.exams.models import BluebookResult
    assert _project(db, scenario, answer) == 0
    db.flush()
    assert db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).count() == 0


def test_projection_is_idempotent_on_resubmission(db, scenario):
    """Re-submitting must update the existing cell, not add a second column."""
    from src.exams.models import BluebookResult
    _project(db, scenario, {"verbal_score": 600, "math_score": 600})
    db.flush()
    _project(db, scenario, {"verbal_score": 700, "math_score": 700})
    db.flush()

    rows = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).all()
    assert len(rows) == 1
    assert rows[0].total_score == 1400


def test_projection_never_raises_on_malformed_content(db, scenario):
    """Projection failure must not cost a student their submission."""
    assert project_bluebook_answers(
        db, assignment=scenario["assignment"], submission=scenario["submission"],
        answers={"task_1": {"verbal_score": 600, "math_score": 600}},
        assignment_content={"tasks": "not-a-list"},
    ) == 0
