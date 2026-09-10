"""Review mode: best-attempt selection, roster scoping, and who may see which group.

The rules that matter here and are easy to get wrong:
  * a *draft* attempt is not a submission and must never appear;
  * among submitted attempts the highest score wins, ties broken by the later one;
  * a roster student with no submitted attempt is reported, not omitted;
  * a teacher sees only their own groups, a curator only their own live groups.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.schemas.models import (  # noqa: F401  (import-order guard: shim first)
    Course,
    Group,
    GroupStudent,
    Lesson,
    Module,
    Step,
    UserInDB,
)
from src.progress.models import QuizAttempt
from src.review import service


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


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

QUIZ_JSON = json.dumps({
    "title": "Vocabulary quiz",
    "questions": [
        {"id": "q1", "question_type": "single_choice", "correct_answer": 1,
         "options": [{"text": "a"}, {"text": "b"}]},
        {"id": "q2", "question_type": "image_content"},
        {"id": "q3", "question_type": "short_answer", "correct_answer": "cat"},
    ],
})


def _user(db, email, name, role="student"):
    u = UserInDB(email=email, name=name, hashed_password="x", role=role, is_active=True)
    db.add(u); db.flush()
    return u


def _group(db, name="G1", teacher=None, curator=None, is_active=True, is_over=False):
    g = Group(name=name, is_active=is_active, is_over=is_over,
              teacher_id=teacher.id if teacher else None,
              curator_id=curator.id if curator else None)
    db.add(g); db.flush()
    return g


def _enroll(db, group, student):
    db.add(GroupStudent(group_id=group.id, student_id=student.id)); db.flush()


def _quiz_step(db, *, kind="unit", content=QUIZ_JSON, title="Vocabulary quiz"):
    """Course -> Module -> Lesson -> quiz Step. Returns (course, lesson, step)."""
    c = Course(title="C"); db.add(c); db.flush()
    m = Module(title="M", course_id=c.id, order_index=0); db.add(m); db.flush()
    l = Lesson(title="Unit 3", module_id=m.id, order_index=0, kind=kind); db.add(l); db.flush()
    s = Step(lesson_id=l.id, title=title, content_type="quiz",
             content_text=content, order_index=0)
    db.add(s); db.flush()
    return c, l, s


def _attempt(db, student, step, lesson, course, *, score, is_draft=False, done=NOW,
             answers='[["q1", 1]]'):
    a = QuizAttempt(user_id=student.id, step_id=step.id, course_id=course.id,
                    lesson_id=lesson.id, quiz_title="Vocabulary quiz",
                    total_questions=2, correct_answers=int(round(score / 50)),
                    score_percentage=score, answers=answers, is_draft=is_draft,
                    completed_at=done, created_at=done)
    db.add(a); db.flush()
    return a


# --- parse helpers -----------------------------------------------------------

def test_question_count_excludes_image_content():
    assert service.quiz_question_count(QUIZ_JSON) == 2


def test_parse_quiz_content_survives_garbage():
    assert service.parse_quiz_content("not json") == {}
    assert service.parse_quiz_content(None) == {}
    assert service.quiz_question_count("not json") == 0


# --- best attempt ------------------------------------------------------------

def test_best_attempt_is_the_highest_score(db):
    student = _user(db, "s1@x.kz", "Student One")
    course, lesson, step = _quiz_step(db)
    _attempt(db, student, step, lesson, course, score=40.0, done=NOW)
    best = _attempt(db, student, step, lesson, course, score=90.0,
                    done=NOW - timedelta(days=1))
    rows = service.best_attempts_for_step(db, step.id, [student.id])
    assert [r.id for r in rows] == [best.id]


def test_score_tie_is_broken_by_the_later_attempt(db):
    student = _user(db, "s2@x.kz", "Student Two")
    course, lesson, step = _quiz_step(db)
    _attempt(db, student, step, lesson, course, score=70.0, done=NOW - timedelta(days=2))
    later = _attempt(db, student, step, lesson, course, score=70.0, done=NOW)
    rows = service.best_attempts_for_step(db, step.id, [student.id])
    assert [r.id for r in rows] == [later.id]


def test_draft_attempts_are_never_returned(db):
    student = _user(db, "s3@x.kz", "Student Three")
    course, lesson, step = _quiz_step(db)
    _attempt(db, student, step, lesson, course, score=100.0, is_draft=True)
    assert service.best_attempts_for_step(db, step.id, [student.id]) == []


def test_only_roster_students_are_returned(db):
    inside = _user(db, "in@x.kz", "Inside")
    outside = _user(db, "out@x.kz", "Outside")
    course, lesson, step = _quiz_step(db)
    _attempt(db, inside, step, lesson, course, score=50.0)
    _attempt(db, outside, step, lesson, course, score=50.0)
    rows = service.best_attempts_for_step(db, step.id, [inside.id])
    assert [r.user_id for r in rows] == [inside.id]


# --- roster ------------------------------------------------------------------

def test_roster_lists_group_students_by_name(db):
    teacher = _user(db, "t@x.kz", "Teacher", role="teacher")
    g = _group(db, teacher=teacher)
    b = _user(db, "b@x.kz", "Borisov")
    a = _user(db, "a@x.kz", "Abenov")
    _enroll(db, g, b); _enroll(db, g, a)
    assert [u.name for u in service.roster_for_group(db, g.id)] == ["Abenov", "Borisov"]


def test_roster_excludes_non_student_members(db):
    # Test/demo groups do carry staff rows in group_students; they must not show up in
    # the roster, in not_submitted, or count toward roster_count/class stats.
    teacher = _user(db, "t2@x.kz", "Teacher Two", role="teacher")
    g = _group(db, teacher=teacher)
    student = _user(db, "stu@x.kz", "Student")
    curator = _user(db, "cur@x.kz", "Curator Member", role="curator")
    _enroll(db, g, student); _enroll(db, g, curator)
    roster = service.roster_for_group(db, g.id)
    assert [u.id for u in roster] == [student.id]


# --- visibility --------------------------------------------------------------

def test_admin_sees_every_group(db):
    admin = _user(db, "adm@x.kz", "Admin", role="admin")
    assert service.visible_group_ids(admin, db) is None


def test_teacher_sees_only_own_groups(db):
    mine = _user(db, "t1@x.kz", "T1", role="teacher")
    theirs = _user(db, "t2@x.kz", "T2", role="teacher")
    g1 = _group(db, name="Mine", teacher=mine)
    g2 = _group(db, name="Theirs", teacher=theirs)
    ids = service.visible_group_ids(mine, db)
    assert g1.id in ids and g2.id not in ids


def test_curator_does_not_see_finished_groups(db):
    curator = _user(db, "c1@x.kz", "C1", role="curator")
    live = _group(db, name="Live", curator=curator)
    over = _group(db, name="Over", curator=curator, is_over=True)
    ids = service.visible_group_ids(curator, db)
    assert live.id in ids and over.id not in ids


def test_student_role_is_refused(db):
    from fastapi import HTTPException
    student = _user(db, "s9@x.kz", "S9")
    with pytest.raises(HTTPException) as exc:
        service.visible_group_ids(student, db)
    assert exc.value.status_code == 403


def test_assert_group_visible_raises_for_another_teachers_group(db):
    from fastapi import HTTPException
    mine = _user(db, "t3@x.kz", "T3", role="teacher")
    theirs = _user(db, "t4@x.kz", "T4", role="teacher")
    _group(db, name="Mine", teacher=mine)
    other = _group(db, name="Theirs", teacher=theirs)
    with pytest.raises(HTTPException) as exc:
        service.assert_group_visible(other.id, mine, db)
    assert exc.value.status_code == 403


# --- unit listing ------------------------------------------------------------

def test_quiz_units_counts_questions_and_submissions(db):
    teacher = _user(db, "t5@x.kz", "T5", role="teacher")
    g = _group(db, teacher=teacher)
    s1 = _user(db, "u1@x.kz", "U1"); s2 = _user(db, "u2@x.kz", "U2")
    _enroll(db, g, s1); _enroll(db, g, s2)
    course, lesson, step = _quiz_step(db)
    _attempt(db, s1, step, lesson, course, score=60.0)
    _attempt(db, s1, step, lesson, course, score=80.0)   # same student twice
    payload = service.quiz_units_for_course(db, course.id, g.id)
    assert payload["roster_count"] == 2
    quiz = payload["units"][0]["quizzes"][0]
    assert quiz["step_id"] == step.id
    assert quiz["question_count"] == 2      # image_content excluded
    assert quiz["submitted_count"] == 1     # distinct students, not attempts


def test_quiz_units_excludes_checkpoint_lessons(db):
    teacher = _user(db, "t6@x.kz", "T6", role="teacher")
    g = _group(db, teacher=teacher)
    course, lesson, step = _quiz_step(db, kind="checkpoint")
    assert service.quiz_units_for_course(db, course.id, g.id)["units"] == []


def test_curator_does_not_see_inactive_groups(db):
    curator = _user(db, "c2@x.kz", "C2", role="curator")
    live = _group(db, name="Live2", curator=curator)
    inactive = _group(db, name="Inactive", curator=curator, is_active=False)
    ids = service.visible_group_ids(curator, db)
    assert live.id in ids and inactive.id not in ids


def test_roster_omits_inactive_students(db):
    teacher = _user(db, "t14@x.kz", "T14", role="teacher")
    g = _group(db, teacher=teacher)
    active = _user(db, "act@x.kz", "Active")
    inactive = _user(db, "inact@x.kz", "Inactive")
    inactive.is_active = False
    db.flush()
    _enroll(db, g, active); _enroll(db, g, inactive)
    ids = [u.id for u in service.roster_for_group(db, g.id)]
    assert active.id in ids and inactive.id not in ids


# --- endpoints ---------------------------------------------------------------

from fastapi import HTTPException  # noqa: E402

from src.review.routes import get_review_quizzes, get_review_session  # noqa: E402


def _grant(db, group, course):
    """Give a group access to a course so check_course_access() passes for its teacher."""
    from src.courses.models import CourseGroupAccess
    # granted_by is NOT NULL in the schema; the identity of the granter doesn't
    # matter for these tests, so a throwaway admin satisfies the FK.
    granter = _user(db, f"granter{course.id}-{group.id}@x.kz", "Granter", role="admin")
    db.add(CourseGroupAccess(group_id=group.id, course_id=course.id,
                             granted_by=granter.id, is_active=True))
    db.flush()


def test_session_returns_roster_attempts_and_not_submitted(db):
    teacher = _user(db, "t7@x.kz", "T7", role="teacher")
    g = _group(db, teacher=teacher)
    done = _user(db, "d@x.kz", "Did It")
    missing = _user(db, "m@x.kz", "Missed It")
    _enroll(db, g, done); _enroll(db, g, missing)
    course, lesson, step = _quiz_step(db)
    _grant(db, g, course)
    _attempt(db, done, step, lesson, course, score=75.0, answers='[["q1", 1]]')

    payload = get_review_session(step_id=step.id, group_id=g.id,
                                 current_user=teacher, db=db)

    assert payload["step"]["step_id"] == step.id
    assert payload["step"]["lesson_title"] == "Unit 3"
    assert payload["step"]["content"]["title"] == "Vocabulary quiz"
    assert [s["student_id"] for s in payload["roster"]] == [done.id, missing.id]
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["student_id"] == done.id
    assert payload["attempts"][0]["answers"] == '[["q1", 1]]'
    assert [s["student_id"] for s in payload["not_submitted"]] == [missing.id]


def test_session_refuses_another_teachers_group(db):
    mine = _user(db, "t8@x.kz", "T8", role="teacher")
    theirs = _user(db, "t9@x.kz", "T9", role="teacher")
    other = _group(db, name="Theirs", teacher=theirs)
    course, lesson, step = _quiz_step(db)
    _grant(db, other, course)
    with pytest.raises(HTTPException) as exc:
        get_review_session(step_id=step.id, group_id=other.id, current_user=mine, db=db)
    assert exc.value.status_code == 403


def test_session_404s_on_a_non_quiz_step(db):
    teacher = _user(db, "t10@x.kz", "T10", role="teacher")
    g = _group(db, teacher=teacher)
    course, lesson, step = _quiz_step(db)
    _grant(db, g, course)
    step.content_type = "text"
    db.flush()
    with pytest.raises(HTTPException) as exc:
        get_review_session(step_id=step.id, group_id=g.id, current_user=teacher, db=db)
    assert exc.value.status_code == 404


def test_session_404s_on_a_missing_step(db):
    teacher = _user(db, "t11@x.kz", "T11", role="teacher")
    g = _group(db, teacher=teacher)
    with pytest.raises(HTTPException) as exc:
        get_review_session(step_id=99999999, group_id=g.id, current_user=teacher, db=db)
    assert exc.value.status_code == 404


def test_quizzes_endpoint_lists_the_course_units(db):
    teacher = _user(db, "t12@x.kz", "T12", role="teacher")
    g = _group(db, teacher=teacher)
    s1 = _user(db, "q1@x.kz", "Q1"); _enroll(db, g, s1)
    course, lesson, step = _quiz_step(db)
    _grant(db, g, course)
    payload = get_review_quizzes(course_id=course.id, group_id=g.id,
                                 current_user=teacher, db=db)
    assert payload["units"][0]["lesson_id"] == lesson.id
    assert payload["units"][0]["quizzes"][0]["step_id"] == step.id


def test_session_refuses_a_course_the_teacher_cannot_see(db):
    """The group is the teacher's, but nobody granted it access to this course."""
    teacher = _user(db, "t13@x.kz", "T13", role="teacher")
    g = _group(db, teacher=teacher)
    course, lesson, step = _quiz_step(db)      # deliberately no _grant()
    with pytest.raises(HTTPException) as exc:
        get_review_session(step_id=step.id, group_id=g.id, current_user=teacher, db=db)
    assert exc.value.status_code == 403
    assert "course" in exc.value.detail.lower()


def test_quizzes_endpoint_refuses_a_student(db):
    student = _user(db, "st@x.kz", "Student")
    g = _group(db)
    course, lesson, step = _quiz_step(db)
    with pytest.raises(HTTPException) as exc:
        get_review_quizzes(course_id=course.id, group_id=g.id,
                           current_user=student, db=db)
    assert exc.value.status_code == 403
