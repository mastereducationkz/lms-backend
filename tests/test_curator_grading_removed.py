"""Curators may no longer grade homework submissions or quiz attempts.

Product decision: `curator` is removed from both grade-write role gates
(PUT /assignments/{id}/submissions/{id}/grade and PUT /progress/quiz-attempts/{id}/grade).
`head_curator` deliberately keeps the capability for now, and teachers/admins
must be unaffected — so the accept cases are asserted too, not just the reject.

The same change made the curator branch of the ungraded-quiz queue
(GET /progress/quiz-attempts/ungraded) read-only, and that queue previously
applied its group filter to teachers only: every other permitted role saw every
attempt platform-wide. The scoping assertions below pin the intended matrix:

    teacher      -> students of the groups they teach   (unchanged)
    curator      -> students of the groups they curate  (new)
    head_curator -> everything                          (unchanged, deliberate)
    admin        -> everything                          (unchanged)

head_curator stays unfiltered on purpose: it is a platform-wide role with no
group-ownership column on `groups`, and the rest of the codebase treats it that
way (check_course_access returns True unconditionally, get_accessible_groups
returns every group). Narrowing it here would make this one endpoint disagree
with the whole system and would hide attempts from students in special groups
or in no group at all.

Fixture strategy — SAVEPOINT isolation, same as tests/test_trial_access_db.py:
the handlers under test call db.commit() internally, so connection-level
rollback alone would not isolate them. Everything is erased at teardown.
"""
import asyncio
import json

import pytest
from fastapi import HTTPException


def _engine_or_none():
    try:
        from sqlalchemy import text

        from src.config import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception:
        return None


ENGINE = _engine_or_none()
pytestmark = pytest.mark.skipif(ENGINE is None, reason="no database available (requires Postgres)")


@pytest.fixture()
def db(monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    # Grading notifies the student by email; never let a test reach Resend.
    import src.services.email_service as email_service

    monkeypatch.setattr(
        email_service, "send_submission_graded_notification", lambda *a, **k: None, raising=False
    )

    conn = ENGINE.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    s = Session()
    s.begin_nested()

    @event.listens_for(s, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.expire_all()
            sess.begin_nested()

    try:
        yield s
    finally:
        event.remove(s, "after_transaction_end", _restart_savepoint)
        s.close()
        txn.rollback()
        conn.close()


# --- seed helpers --------------------------------------------------------------

_QUIZ_CONTENT = json.dumps(
    {
        "questions": [
            {
                "id": "q1",
                "question_type": "long_text",
                "question_text": "Explain the answer",
                "points": 10,
            }
        ]
    }
)


def _user(db, email, role, name=None):
    from src.schemas.models import UserInDB

    u = UserInDB(
        email=email,
        name=name or role,
        hashed_password="x",
        role=role,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _course_with_quiz_step(db, title, teacher_id=None):
    """Course -> Module -> Lesson -> Step, where the step holds a long_text quiz."""
    from src.schemas.models import Course, Lesson, Module, Step

    course = Course(title=title, teacher_id=teacher_id, is_active=True)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title=f"{title} M", order_index=0)
    db.add(module)
    db.flush()
    lesson = Lesson(module_id=module.id, title=f"{title} L", order_index=0)
    db.add(lesson)
    db.flush()
    step = Step(
        lesson_id=lesson.id,
        title=f"{title} quiz",
        content_type="quiz",
        content_text=_QUIZ_CONTENT,
        order_index=0,
    )
    db.add(step)
    db.flush()
    return course, lesson, step


def _group(db, name, *, teacher_id=None, curator_id=None, is_special=False):
    from src.schemas.models import Group

    g = Group(
        name=name,
        teacher_id=teacher_id,
        curator_id=curator_id,
        is_active=True,
        is_special=is_special,
    )
    db.add(g)
    db.flush()
    return g


def _enroll(db, group, student, course, granted_by):
    from src.schemas.models import CourseGroupAccess, GroupStudent

    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.add(
        CourseGroupAccess(
            course_id=course.id,
            group_id=group.id,
            granted_by=granted_by.id,
            is_active=True,
        )
    )
    db.flush()


def _ungraded_attempt(db, student, course, lesson, step):
    from src.progress.models import QuizAttempt

    a = QuizAttempt(
        user_id=student.id,
        step_id=step.id,
        course_id=course.id,
        lesson_id=lesson.id,
        total_questions=1,
        correct_answers=0,
        score_percentage=0.0,
        answers=json.dumps({"q1": "a long free-text answer"}),
        is_draft=False,
        is_graded=False,
    )
    db.add(a)
    db.flush()
    return a


def _assignment_with_submission(db, lesson, group, student):
    from src.schemas.models import Assignment, AssignmentSubmission

    assignment = Assignment(
        lesson_id=lesson.id,
        group_id=group.id,
        title="Homework 1",
        assignment_type="text",
        content="{}",
        max_score=100,
        is_active=True,
    )
    db.add(assignment)
    db.flush()
    submission = AssignmentSubmission(
        assignment_id=assignment.id,
        user_id=student.id,
        answers="{}",
        max_score=100,
        is_graded=False,
    )
    db.add(submission)
    db.flush()
    return assignment, submission


@pytest.fixture()
def world(db):
    """One normal group (teacher+curator) and one special group, each with a
    student sitting on an ungraded long_text quiz attempt."""
    import types

    admin = _user(db, "grading-roles-admin@x.kz", "admin")
    teacher = _user(db, "grading-roles-teacher@x.kz", "teacher")
    curator = _user(db, "grading-roles-curator@x.kz", "curator")
    head_curator = _user(db, "grading-roles-headcurator@x.kz", "head_curator")
    student = _user(db, "grading-roles-student@x.kz", "student")
    outsider = _user(db, "grading-roles-outsider@x.kz", "student")

    course, lesson, step = _course_with_quiz_step(db, "Roles Course", teacher_id=teacher.id)
    group = _group(db, "Roles Group", teacher_id=teacher.id, curator_id=curator.id)
    _enroll(db, group, student, course, granted_by=admin)

    # A group nobody in `world` teaches or curates, and that head curators do not
    # oversee (is_special=True) — the control for every scoping assertion.
    other_course, other_lesson, other_step = _course_with_quiz_step(db, "Special Course")
    special_group = _group(db, "Special Group", is_special=True)
    _enroll(db, special_group, outsider, other_course, granted_by=admin)

    attempt = _ungraded_attempt(db, student, course, lesson, step)
    other_attempt = _ungraded_attempt(db, outsider, other_course, other_lesson, other_step)

    assignment, submission = _assignment_with_submission(db, lesson, group, student)

    return types.SimpleNamespace(
        db=db,
        admin=admin,
        teacher=teacher,
        curator=curator,
        head_curator=head_curator,
        student=student,
        outsider=outsider,
        course=course,
        lesson=lesson,
        step=step,
        group=group,
        attempt=attempt,
        other_attempt=other_attempt,
        assignment=assignment,
        submission=submission,
    )


# --- call helpers --------------------------------------------------------------

def _grade_submission(world, actor, score=90):
    from src.assignments.routes import assignments as assignments_routes
    from src.schemas.models import GradeSubmissionSchema

    return asyncio.run(
        assignments_routes.grade_submission(
            assignment_id=world.assignment.id,
            submission_id=world.submission.id,
            grade_data=GradeSubmissionSchema(score=score, feedback="ok"),
            current_user=actor,
            db=world.db,
        )
    )


def _grade_quiz(world, actor, score=90.0):
    from src.progress.routes import progress as progress_routes
    from src.schemas.models import QuizAttemptGradeSchema

    return progress_routes.grade_quiz_attempt(
        attempt_id=world.attempt.id,
        grade_data=QuizAttemptGradeSchema(score_percentage=score, correct_answers=1, feedback="ok"),
        current_user=actor,
        db=world.db,
    )


def _ungraded_ids(world, actor):
    from src.progress.routes import progress as progress_routes

    rows = progress_routes.get_ungraded_attempts(current_user=actor, db=world.db, graded=None)
    return {r["id"] for r in rows}


# --- curator is rejected by both grade endpoints --------------------------------

def test_curator_cannot_grade_homework_submission(world):
    with pytest.raises(HTTPException) as exc:
        _grade_submission(world, world.curator)
    assert exc.value.status_code == 403
    assert "curators" not in exc.value.detail.lower().replace("head curators", "")

    world.db.refresh(world.submission)
    assert world.submission.is_graded is False
    assert world.submission.score is None


def test_curator_cannot_grade_quiz_attempt(world):
    with pytest.raises(HTTPException) as exc:
        _grade_quiz(world, world.curator)
    assert exc.value.status_code == 403

    world.db.refresh(world.attempt)
    assert world.attempt.is_graded is False
    assert world.attempt.graded_by is None


def test_curator_of_the_students_own_group_is_still_rejected(world):
    """The curator here curates the group the submission's student belongs to —
    the exact case the removed curator-scoping block used to allow."""
    assert world.group.curator_id == world.curator.id
    with pytest.raises(HTTPException) as exc:
        _grade_submission(world, world.curator)
    assert exc.value.status_code == 403


# --- teacher / admin / head_curator still grade ---------------------------------

def test_teacher_can_still_grade_homework_submission(world):
    result = _grade_submission(world, world.teacher, score=80)
    assert result.score == 80
    assert result.is_graded is True
    assert result.graded_by == world.teacher.id


def test_admin_can_still_grade_homework_submission(world):
    result = _grade_submission(world, world.admin, score=70)
    assert result.score == 70
    assert result.graded_by == world.admin.id


def test_head_curator_can_still_grade_homework_submission(world):
    """Deliberate carve-out: head_curator keeps grading for now."""
    result = _grade_submission(world, world.head_curator, score=60)
    assert result.score == 60
    assert result.graded_by == world.head_curator.id


def test_teacher_can_still_grade_quiz_attempt(world):
    result = _grade_quiz(world, world.teacher, score=88.0)
    assert result.is_graded is True
    assert result.graded_by == world.teacher.id
    assert result.score_percentage == 88.0


def test_admin_can_still_grade_quiz_attempt(world):
    result = _grade_quiz(world, world.admin, score=77.0)
    assert result.graded_by == world.admin.id


def test_head_curator_can_still_grade_quiz_attempt(world):
    result = _grade_quiz(world, world.head_curator, score=66.0)
    assert result.graded_by == world.head_curator.id


# --- ungraded queue scoping -----------------------------------------------------

@pytest.mark.parametrize("role", ["admin", "head_curator"])
def test_ungraded_queue_is_unscoped_for_platform_wide_roles(world, role):
    """Admins and head curators both see every attempt — head_curator is
    deliberately NOT narrowed here (see the module docstring)."""
    ids = _ungraded_ids(world, getattr(world, role))
    assert {world.attempt.id, world.other_attempt.id} <= ids


def test_ungraded_queue_is_scoped_for_curator(world):
    """Curators keep read-only visibility, but only for their own groups."""
    ids = _ungraded_ids(world, world.curator)
    assert world.attempt.id in ids
    assert world.other_attempt.id not in ids


def test_ungraded_queue_stays_scoped_for_teacher(world):
    ids = _ungraded_ids(world, world.teacher)
    assert world.attempt.id in ids
    assert world.other_attempt.id not in ids


def test_ungraded_queue_hides_other_curators_groups(world):
    """A curator with no groups at all sees nothing, rather than everything."""
    stranger = _user(world.db, "grading-roles-curator2@x.kz", "curator")
    ids = _ungraded_ids(world, stranger)
    assert world.attempt.id not in ids
    assert world.other_attempt.id not in ids
