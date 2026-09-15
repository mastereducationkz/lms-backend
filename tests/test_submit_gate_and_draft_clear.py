import json
import asyncio
import io
import pytest
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from src.schemas.models import (
    UserInDB, Group, GroupStudent, Course, Module, Lesson,
)
from src.assignments.models import (
    Assignment, AssignmentSubmission, AssignmentLinkedLesson, AssignmentDraft,
)
from src.progress.models import StudentProgress
from src.utils.auth_utils import hash_password

from src.assignments.routes.assignments import submit_assignment, save_draft
from src.assignments.schemas import SubmitAssignmentSchema, DraftUpsertSchema
from src.admin.routes.media import upload_submission_file


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


def _student(db, email="drafts-student@test.local"):
    u = UserInDB(email=email, name="drafts-student", role="student",
                 hashed_password=hash_password("x"))
    db.add(u); db.flush()
    return u


def _group_with_student(db, student):
    g = Group(name="drafts-grp")
    db.add(g); db.flush()
    db.add(GroupStudent(group_id=g.id, student_id=student.id)); db.flush()
    return g


def _unit_assignment(db, group, *, linked_lessons=(), atype="multi_task"):
    """Assignment linked to `group`, of type `atype`, linked to given Lesson objects."""
    a = Assignment(title="Unit HW", assignment_type=atype,
                   content=json.dumps({"tasks": []}), max_score=10,
                   group_id=group.id, is_active=True, is_hidden=False)
    db.add(a); db.flush()
    for lesson in linked_lessons:
        db.add(AssignmentLinkedLesson(assignment_id=a.id, lesson_id=lesson.id))
    db.flush()
    return a


def _lesson(db, title="Unit A"):
    c = Course(title="C"); db.add(c); db.flush()
    m = Module(title="M", course_id=c.id); db.add(m); db.flush()
    l = Lesson(title=title, module_id=m.id); db.add(l); db.flush()
    return c, l


def _complete_lesson(db, student, course, lesson):
    db.add(StudentProgress(user_id=student.id, course_id=course.id,
                           lesson_id=lesson.id, status="completed",
                           completed_at=datetime.now(timezone.utc)))
    db.flush()


def test_submit_blocked_when_units_incomplete(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A")
    a = _unit_assignment(db, group, linked_lessons=(l1,))  # not completed
    with pytest.raises(HTTPException) as ei:
        submit_assignment(a.id, SubmitAssignmentSchema(answers={"tasks": {}}), student, db)
    assert ei.value.status_code == 409


def test_submit_clears_draft_when_ready(db):
    student = _student(db); group = _group_with_student(db, student)
    c1, l1 = _lesson(db, "Unit A")
    a = _unit_assignment(db, group, linked_lessons=(l1,))
    _complete_lesson(db, student, c1, l1)
    save_draft(a.id, DraftUpsertSchema(answers={"tasks": {"1": "x"}}), student, db)
    submit_assignment(a.id, SubmitAssignmentSchema(answers={"tasks": {"1": "x"}}), student, db)
    assert db.query(AssignmentDraft).filter(
        AssignmentDraft.assignment_id == a.id,
        AssignmentDraft.user_id == student.id).first() is None


def test_late_submission_is_accepted_and_marked_late(db):
    student = _student(db, email="late-submit@test.local")
    group = _group_with_student(db, student)
    assignment = _unit_assignment(db, group, atype="free_text")
    assignment.due_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.flush()

    submission = submit_assignment(
        assignment.id,
        SubmitAssignmentSchema(answers={"text": "Submitted after the deadline"}),
        student,
        db,
    )

    assert submission.is_late is True


def test_file_upload_after_deadline_can_be_submitted_and_is_marked_late(db, monkeypatch):
    """Exercise the registered media endpoint, not only the final submit route."""
    student = _student(db, email="late-file-submit@test.local")
    group = _group_with_student(db, student)
    assignment = _unit_assignment(db, group, atype="file_upload")
    assignment.due_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.flush()
    monkeypatch.setattr(
        "src.admin.routes.media.storage_service.save",
        lambda path, content, content_type: f"https://files.test/{path}",
    )
    upload = UploadFile(
        file=io.BytesIO(b"my answer"),
        filename="answer.txt",
        headers=Headers({"content-type": "text/plain"}),
    )

    uploaded = asyncio.run(upload_submission_file(assignment.id, upload, student, db))
    submission = submit_assignment(
        assignment.id,
        SubmitAssignmentSchema(
            answers={"text": "File attached"},
            file_url=uploaded["file_url"],
            submitted_file_name=uploaded["filename"],
        ),
        student,
        db,
    )

    assert uploaded["file_url"].endswith("answer.txt")
    assert submission.is_late is True


def test_late_submission_still_respects_fixed_attempt_limit(db):
    student = _student(db, email="late-attempt-limit@test.local")
    group = _group_with_student(db, student)
    assignment = _unit_assignment(db, group, atype="free_text")
    assignment.due_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assignment.max_attempts = 1
    db.flush()

    submit_assignment(assignment.id, SubmitAssignmentSchema(answers={"text": "first"}), student, db)
    with pytest.raises(HTTPException) as exc:
        submit_assignment(assignment.id, SubmitAssignmentSchema(answers={"text": "second"}), student, db)

    assert exc.value.status_code == 400


def test_late_submission_cannot_replace_a_final_grade_without_reopen(db):
    student = _student(db, email="late-graded@test.local")
    group = _group_with_student(db, student)
    assignment = _unit_assignment(db, group, atype="free_text")
    assignment.due_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assignment.max_attempts = None
    db.flush()
    first = submit_assignment(assignment.id, SubmitAssignmentSchema(answers={"text": "first"}), student, db)
    # submit_assignment returns a response schema (a copy); the grade has to land on the stored
    # row, which is what the next submission's gate reads.
    stored = db.get(AssignmentSubmission, first.id)
    stored.is_graded = True
    db.flush()

    with pytest.raises(HTTPException) as exc:
        submit_assignment(assignment.id, SubmitAssignmentSchema(answers={"text": "replacement"}), student, db)

    assert exc.value.status_code == 409
