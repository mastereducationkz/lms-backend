"""Student results report: aggregation correctness, access matrix, PDF smoke.

The access matrix is the point of the feature: admin/head_curator/head_teacher see any
student, a curator only their own groups' students, and regular teachers see nothing —
the report bundles cross-course data beyond a single teacher's scope.
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from src.schemas.models import (  # noqa: F401  (import-order guard: shim first)
    Assignment,
    AssignmentSubmission,
    Attendance,
    Course,
    Enrollment,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    Lesson,
    Module,
    Step,
    UserInDB,
)
from src.auth.models import PointHistory
from src.exams.models import BluebookResult
from src.gamification.models import DailyQuestionCompletion
from src.progress.models import QuizAttempt
from src.reports.pdf import render_student_report_pdf
from src.reports.routes import _require_report_access
from src.reports.services import build_student_report, build_submission_detail


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


def _user(db, email, role, name=None):
    u = UserInDB(email=email, name=name or email.split("@")[0], hashed_password="x",
                 role=role, is_active=True)
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def seeded(db):
    """One student with one row in every report surface, plus the role cast."""
    student = _user(db, "report.student@test.local", "student", "Тестовый Ученик")
    other_student = _user(db, "report.other@test.local", "student")
    curator = _user(db, "report.curator@test.local", "curator")
    other_curator = _user(db, "report.curator2@test.local", "curator")
    teacher = _user(db, "report.teacher@test.local", "teacher")
    admin = _user(db, "report.admin@test.local", "admin")
    head_curator = _user(db, "report.hc@test.local", "head_curator")
    head_teacher = _user(db, "report.ht@test.local", "head_teacher")

    group = Group(name="Report Test Group", curator_id=curator.id, teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))

    course = Course(title="Report Course", description="", teacher_id=teacher.id, is_active=True)
    db.add(course)
    db.flush()
    module = Module(course_id=course.id, title="Unit 1", order_index=1)
    db.add(module)
    db.flush()
    lesson = Lesson(module_id=module.id, title="Unit 1: Basics", order_index=1)
    db.add(lesson)
    db.flush()
    step = Step(lesson_id=lesson.id, title="Quiz", content_type="quiz", order_index=1)
    db.add(step)
    db.add(Enrollment(user_id=student.id, course_id=course.id, is_active=True))
    db.flush()

    # Homework: one graded, one not submitted.
    graded = Assignment(title="HW graded", description="", assignment_type="test", content="{}",
                        max_score=10,
                        group_id=group.id, is_active=True,
                        due_date=datetime(2026, 8, 1, 10, 0))
    unsubmitted = Assignment(title="HW missing", description="", assignment_type="test", content="{}",
                             max_score=20,
                             group_id=group.id, is_active=True,
                             due_date=datetime(2026, 8, 10, 10, 0))
    db.add_all([graded, unsubmitted])
    db.flush()
    db.add(AssignmentSubmission(
        assignment_id=graded.id, user_id=student.id, answers="{}", max_score=10,
        submitted_at=datetime(2026, 7, 31, 12, 0), is_graded=True, score=8,
        graded_at=datetime(2026, 8, 1, 12, 0),
    ))

    # Quizzes: two completed attempts and a draft.
    for pct, correct, draft in ((80.0, 8, False), (60.0, 6, False), (10.0, 1, True)):
        db.add(QuizAttempt(
            user_id=student.id, step_id=step.id, course_id=course.id,
            lesson_id=lesson.id, quiz_title="Quiz", total_questions=10,
            correct_answers=correct, score_percentage=pct, is_draft=draft,
            completed_at=None if draft else datetime(2026, 8, 5, 12, 0),
        ))

    # Attendance: present, late, absent, and one future-dated "absent" artifact
    # (attendance recorded, then the event was rescheduled months ahead).
    now = datetime.utcnow()
    statuses = [("present", now - timedelta(days=5)), ("late", now - timedelta(days=3)),
                ("absent", now - timedelta(days=1)), ("absent", now + timedelta(days=60))]
    for status, start in statuses:
        ev = Event(title=f"Lesson {status}", event_type="class", is_active=True,
                   start_datetime=start, end_datetime=start + timedelta(hours=2),
                   created_by=teacher.id)
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
        db.add(Attendance(event_id=ev.id, user_id=student.id, status=status))

    db.add(BluebookResult(student_id=student.id, group_id=group.id, test_number=7,
                          verbal_score=550, math_score=590, total_score=1140,
                          taken_at=date(2026, 8, 16), source="homework"))
    db.add(PointHistory(user_id=student.id, amount=100, reason="course_quiz"))
    db.add(PointHistory(user_id=student.id, amount=50, reason="homework"))
    db.add(DailyQuestionCompletion(user_id=student.id, completed_date=date(2026, 8, 20)))
    db.flush()

    return {
        "student": student, "other_student": other_student, "curator": curator,
        "other_curator": other_curator, "teacher": teacher, "admin": admin,
        "head_curator": head_curator, "head_teacher": head_teacher, "group": group,
    }


def test_report_aggregates_every_surface(db, seeded):
    report = build_student_report(db, seeded["student"].id)

    assert report["student"]["name"] == "Тестовый Ученик"
    assert [g["name"] for g in report["student"]["groups"]] == ["Report Test Group"]

    hw = report["homework"]
    assert hw["assigned"] == 2
    assert hw["graded"] == 1
    assert hw["earned_score"] == 8
    assert hw["max_score"] == 30
    statuses = {i["title"]: i["status"] for i in hw["items"]}
    assert statuses == {"HW graded": "graded", "HW missing": "not_submitted"}
    graded_item = next(i for i in hw["items"] if i["title"] == "HW graded")
    assert graded_item["submission"]["graded_at"] is not None
    missing_item = next(i for i in hw["items"] if i["title"] == "HW missing")
    assert missing_item["submission"] is None

    (quiz_course,) = report["quizzes"]
    assert quiz_course["total_attempts"] == 3
    assert quiz_course["completed_attempts"] == 2
    assert quiz_course["draft_attempts"] == 1
    assert quiz_course["average_pct"] == 70.0
    (section,) = quiz_course["sections"]
    assert section["lesson_title"] == "Unit 1: Basics"
    assert section["best_pct"] == 80.0
    assert [a["correct"] for a in section["attempt_details"]] == [8, 6]

    assert [b["total"] for b in report["bluebook"]] == [1140]
    assert report["exams"]["results"] == []

    att = report["attendance"]
    # The future-dated absent row is a rescheduling artifact and must not count.
    assert att["marked_total"] == 3
    assert att["attended"] == 2
    assert att["late"] == 1
    assert att["absent"] == 1
    assert len(att["absences"]) == 1

    act = report["activity"]
    assert act["points_total"] == 150
    assert act["points_by_reason"] == {"course_quiz": 100, "homework": 50}
    assert act["daily_questions_completed"] == 1


def test_report_404_for_non_student(db, seeded):
    with pytest.raises(HTTPException) as exc:
        build_student_report(db, seeded["curator"].id)
    assert exc.value.status_code == 404


def test_access_matrix(db, seeded):
    student_id = seeded["student"].id

    # Full-access roles: any student.
    for role in ("admin", "head_curator", "head_teacher"):
        _require_report_access(student_id, seeded[role], db)

    # Curator of the student's group: allowed.
    _require_report_access(student_id, seeded["curator"], db)

    # Curator of a different group: denied.
    with pytest.raises(HTTPException) as exc:
        _require_report_access(student_id, seeded["other_curator"], db)
    assert exc.value.status_code == 403

    # Regular teacher (even the group's teacher), students, and the student
    # themselves: denied.
    for who in ("teacher", "other_student", "student"):
        with pytest.raises(HTTPException) as exc:
            _require_report_access(student_id, seeded[who], db)
        assert exc.value.status_code == 403


def test_pdf_renders(db, seeded):
    report = build_student_report(db, seeded["student"].id)
    # The routes attach external weekly tests; the PDF must render them too,
    # including markdown-ish feedback text with characters HTML would eat.
    report["weekly_tests"] = {
        "sat": [{
            "week_label": "22.08-23.08", "completed_at": "2026-08-22T18:00:00",
            "math": {"correct": 17, "total": 22, "pct": 77.27,
                     "feedback": "**1) SUMMARY**\nScored 17 < 22 & improving."},
            "verbal": {"correct": 11, "total": 27, "pct": 40.74, "feedback": None},
        }],
        "ielts": [{
            "set_id": 12, "week_label": "22.08-23.08",
            "listening_band": 5.5, "reading_band": 6.5, "writing_band": None,
            "speaking_band": 5.0, "overall_band": 5.5, "speaking_status": None,
            "feedback": {"listening": "Вы набрали 18 из 40.", "reading": None,
                         "writing": {"task1": None, "task2": None},
                         "speaking": {"overall": "Frequent pauses."}},
        }],
        "nuet": [],
        "errors": ["nuet: timeout"],
    }
    buffer = render_student_report_pdf(report)
    data = buffer.getvalue()
    assert data.startswith(b"%PDF")
    assert len(data) > 1000


def test_group_program_bucketing(db, seeded):
    from src.reports.external import _group_programs

    ielts_group = Group(name="IELTS August 2 2026 - Said", program_type="general_english")
    db.add(ielts_group)
    db.flush()
    db.add(GroupStudent(group_id=ielts_group.id, student_id=seeded["student"].id))
    db.flush()

    buckets = _group_programs(db, seeded["student"].id)
    # "Report Test Group" matches no program; the IELTS group matches by name.
    assert [g.name for g in buckets["ielts"]] == ["IELTS August 2 2026 - Said"]
    assert buckets["nuet"] == []


def test_submission_detail_pairs_tasks_with_answers(db, seeded):
    import json
    student = seeded["student"]
    assignment = Assignment(
        title="MT homework", description="", assignment_type="multi_task",
        content=json.dumps({"tasks": [
            {"id": "task_a", "task_type": "text_task", "title": "Essay", "points": 10,
             "content": {"question": "Write about your day"}},
            {"id": "task_b", "task_type": "course_unit", "title": "Unit", "points": 5, "content": {}},
        ]}),
        max_score=15, group_id=seeded["group"].id, is_active=True,
        due_date=datetime(2026, 8, 20, 10, 0),
    )
    db.add(assignment)
    db.flush()
    submission = AssignmentSubmission(
        assignment_id=assignment.id, user_id=student.id, max_score=15,
        answers=json.dumps({"task_a": {"text_response": "My day was fine", "completed": True},
                            "task_b": {"completed": True}}),
        submitted_at=datetime(2026, 8, 19, 12, 0), is_graded=True, score=14,
        feedback="Good work",
    )
    db.add(submission)
    db.flush()

    detail = build_submission_detail(db, student.id, submission.id)
    assert detail["assignment"]["title"] == "MT homework"
    assert [t["id"] for t in detail["assignment"]["tasks"]] == ["task_a", "task_b"]
    assert detail["assignment"]["tasks"][0]["question"] == "Write about your day"
    assert detail["submission"]["answers"]["task_a"]["text_response"] == "My day was fine"
    assert detail["submission"]["feedback"] == "Good work"

    # Another student's submission id must 404, not leak.
    with pytest.raises(HTTPException) as exc:
        build_submission_detail(db, seeded["other_student"].id, submission.id)
    assert exc.value.status_code == 404
