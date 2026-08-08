"""Bluebook homework task: content validation and the queryable projection.

Mirrors the style of tests/test_audio_task_multitask.py - the validation half is pure
and runs without Postgres, which matters because a third of this suite skips silently
when no database is reachable.
"""
from datetime import date, datetime, timezone
from pathlib import Path

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


def _stub_report(monkeypatch, *, verbal, math, test_number=7, report_date=None,
                 student_name="Test Student"):
    """Stand in for the stored-PDF re-parse.

    Bluebook scores now come only from the official College Board report, which the
    server re-parses from storage. These tests exercise the projection's behaviour, not
    pypdf, so the parse result is stubbed; the parser itself is covered exhaustively in
    tests/test_bluebook_pdf.py against real captured reports.
    """
    from src.exams import projection as proj

    class _Parsed:
        pass

    _Parsed.test_number = test_number
    _Parsed.verbal_score = verbal
    _Parsed.math_score = math
    _Parsed.total_score = verbal + math
    _Parsed.report_date = report_date
    _Parsed.student_name = student_name

    monkeypatch.setattr(proj, "_parse_stored_report", lambda key: _Parsed())


REPORT_KEY = "/uploads/bluebook_report/report.pdf"


def test_projection_writes_a_row_from_the_official_report(db, scenario, monkeypatch):
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=640, math=780)
    assert _project(db, scenario, {"report_key": REPORT_KEY}) == 1
    db.flush()

    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.verbal_score == 640
    assert row.math_score == 780
    assert row.total_score == 1420          # matches the reference sheet
    assert row.group_id == scenario["group"].id
    assert row.source == "homework"
    assert row.report_url == REPORT_KEY


def test_projection_derives_the_total_and_ignores_any_client_total(db, scenario, monkeypatch):
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600)
    _project(db, scenario, {"report_key": REPORT_KEY, "total_score": 9999})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.total_score == 1200


def test_projection_dates_the_row_from_the_report_when_it_has_one(db, scenario, monkeypatch):
    """The report's own date beats the homework due date - it is when the test was
    actually sat."""
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600, report_date=date(2026, 7, 1))
    _project(db, scenario, {"report_key": REPORT_KEY})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.taken_at == date(2026, 7, 1)


def test_projection_falls_back_to_the_due_date_when_the_report_has_none(db, scenario, monkeypatch):
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600, report_date=None)
    _project(db, scenario, {"report_key": REPORT_KEY})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.taken_at == date(2026, 7, 6)


def test_projection_records_the_name_printed_on_the_report(db, scenario, monkeypatch):
    """Stored so staff can spot a report submitted on someone else's behalf."""
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600, student_name="Someone Else")
    _project(db, scenario, {"report_key": REPORT_KEY})
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.report_student_name == "Someone Else"


def test_projection_still_captures_an_attached_screenshot(db, scenario, monkeypatch):
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600)
    _project(db, scenario, {
        "report_key": REPORT_KEY,
        "files": [{"file_url": "/uploads/x/shot.png", "file_name": "shot.png"}],
    })
    db.flush()
    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert row.screenshot_url == "/uploads/x/shot.png"


def test_projection_is_idempotent_on_resubmission(db, scenario, monkeypatch):
    """Re-submitting must update the existing cell, not add a second column."""
    from src.exams.models import BluebookResult
    _stub_report(monkeypatch, verbal=600, math=600)
    _project(db, scenario, {"report_key": REPORT_KEY})
    db.flush()
    _stub_report(monkeypatch, verbal=700, math=700)
    _project(db, scenario, {"report_key": REPORT_KEY})
    db.flush()

    rows = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).all()
    assert len(rows) == 1
    assert rows[0].total_score == 1400


@pytest.mark.parametrize("answer", [
    {},                                          # nothing at all
    {"report_key": ""},                          # blank key
    {"verbal_score": 640, "math_score": 780},    # typed scores, no report
])
def test_projection_records_nothing_without_an_official_report(db, scenario, answer):
    from src.exams.models import BluebookResult
    assert _project(db, scenario, answer) == 0
    db.flush()
    assert db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).count() == 0


def test_projection_never_raises_on_malformed_content(db, scenario):
    """Projection failure must not cost a student their submission."""
    assert project_bluebook_answers(
        db, assignment=scenario["assignment"], submission=scenario["submission"],
        answers={"task_1": {"verbal_score": 600, "math_score": 600}},
        assignment_content={"tasks": "not-a-list"},
    ) == 0


# --------------------------------------------------------------------------------------
# Official PDF report is the ONLY source of a Bluebook score
# --------------------------------------------------------------------------------------

def test_projection_ignores_client_supplied_scores_without_a_report(db, scenario):
    """Students can no longer type a Bluebook score. Without a report_key there is
    nothing to record, however plausible the numbers look."""
    from src.exams.models import BluebookResult
    assert _project(db, scenario, {"verbal_score": 800, "math_score": 800}) == 0
    db.flush()
    assert db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).count() == 0


def test_projection_reads_scores_from_the_stored_pdf_not_the_payload(db, scenario, tmp_path,
                                                                    monkeypatch):
    """The submission carries a storage key; the server re-parses that file. A tampered
    payload claiming 1600 must not change what is recorded."""
    from src.exams.models import BluebookResult
    from src.exams import projection as proj

    # A genuine report for test 7: RW 720, Math 200, total 920.
    report_text = (Path(__file__).parent / "fixtures" / "bluebook" / "practice_7.txt").read_text()

    class _Parsed:
        test_number, verbal_score, math_score = 7, 720, 200
        total_score, report_date, student_name = 920, date(2025, 4, 19), "Test Student"

    monkeypatch.setattr(proj, "_parse_stored_report", lambda key: _Parsed())

    # Point the assignment at test 7 so the report matches what was assigned.
    import json as _json
    scenario["assignment"].content = _json.dumps(
        {"tasks": [{"id": "task_1", "task_type": "bluebook_task", "title": "BB7",
                    "order_index": 0, "points": 10, "content": {"test_number": 7}}]}
    )
    db.flush()
    scenario["content"] = _json.loads(scenario["assignment"].content)

    written = _project(db, scenario, {
        "report_key": "/uploads/bluebook_report/x.pdf",
        "verbal_score": 800, "math_score": 800,   # tampered, must be ignored
    })
    assert written == 1
    db.flush()

    row = db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).one()
    assert (row.verbal_score, row.math_score, row.total_score) == (720, 200, 920)
    assert row.report_url == "/uploads/bluebook_report/x.pdf"
    assert row.report_date == date(2025, 4, 19)
    assert row.report_student_name == "Test Student"
    assert report_text  # fixture is present and readable


def test_projection_refuses_a_report_for_the_wrong_test(db, scenario, monkeypatch):
    """Assignment asks for #7; the student uploads their #9 report."""
    from src.exams.models import BluebookResult
    from src.exams import projection as proj

    class _Parsed:
        test_number, verbal_score, math_score = 9, 700, 700
        total_score, report_date, student_name = 1400, date(2025, 5, 2), "Test Student"

    monkeypatch.setattr(proj, "_parse_stored_report", lambda key: _Parsed())
    assert _project(db, scenario, {"report_key": "/uploads/bluebook_report/y.pdf"}) == 0
    db.flush()
    assert db.query(BluebookResult).filter(
        BluebookResult.student_id == scenario["student"].id).count() == 0


def test_projection_skips_when_the_stored_report_cannot_be_read(db, scenario, monkeypatch):
    from src.exams import projection as proj
    monkeypatch.setattr(proj, "_parse_stored_report", lambda key: None)
    assert _project(db, scenario, {"report_key": "/uploads/bluebook_report/gone.pdf"}) == 0
