"""Student results report: one aggregation shared by the JSON endpoint and the PDF.

Pulls every result surface the LMS tracks for one student — homework, quizzes,
Bluebook practice tests, official exam results, course progress, attendance and
activity points — into a single serializable dict. Route-level access control lives
in ``src/reports/routes.py``; this module assumes the caller is already authorized.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.schemas.models import (
    Assignment,
    AssignmentSubmission,
    AssignmentZeroSubmission,
    Attendance,
    Course,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    Lesson,
    Module,
    UserInDB,
)
from src.progress.models import QuizAttempt, StudentCourseSummary
from src.exams.models import BluebookResult, ExamResult
from src.gamification.models import DailyQuestionCompletion
from src.auth.models import PointHistory
from src.assignments.models import AssignmentLinkedLesson


def _iso(value) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _student_or_404(db: Session, student_id: int) -> UserInDB:
    student = db.query(UserInDB).filter(
        UserInDB.id == student_id, UserInDB.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


def _homework_section(db: Session, student_id: int) -> Dict[str, Any]:
    # Imported lazily: assignments routes import broadly and importing them at module
    # load time creates a cycle through src.routes.
    from src.assignments.routes.assignments import (
        _student_assignments,
        _build_student_assignment_status,
        assignment_visible_to_student,
    )

    assignments: List[Assignment] = [
        a for a in _student_assignments(db, student_id).all()
        if assignment_visible_to_student(student_id, a, db)
    ]
    assignment_ids = [a.id for a in assignments]

    submissions = {
        s.assignment_id: s
        for s in db.query(AssignmentSubmission).filter(
            AssignmentSubmission.user_id == student_id,
            AssignmentSubmission.assignment_id.in_(assignment_ids or [0]),
        ).all()
    }
    linked_lessons = {
        link.assignment_id: link.lesson_id
        for link in db.query(AssignmentLinkedLesson).filter(
            AssignmentLinkedLesson.assignment_id.in_(assignment_ids or [0])
        ).all()
    }

    items = []
    for a in assignments:
        submission = submissions.get(a.id)
        status = _build_student_assignment_status(submission, a)
        items.append({
            "id": a.id,
            "title": (a.title or "").strip(),
            "due_date": _iso(a.due_date),
            "max_score": a.max_score,
            "lesson_id": linked_lessons.get(a.id),
            "status": status["status"],
            "score": status["score"],
            "submitted_at": _iso(status["submitted_at"]),
            # Submission drill-down for the report page. file_url is exposed to staff
            # the same way the grading endpoints already expose it.
            "submission": {
                "id": submission.id,
                "graded_at": _iso(submission.graded_at),
                "is_late": bool(submission.is_late),
                "feedback": submission.feedback,
                "file_url": submission.file_url,
                "file_name": submission.submitted_file_name,
            } if submission else None,
        })
    items.sort(key=lambda i: (i["due_date"] or "9999", i["title"]))

    graded = [i for i in items if i["status"] == "graded"]
    submitted = [i for i in items if i["status"] == "submitted"]
    return {
        "assigned": len(items),
        "submitted": len(graded) + len(submitted),
        "graded": len(graded),
        "earned_score": sum(i["score"] or 0 for i in graded),
        "max_score": sum(i["max_score"] or 0 for i in items),
        "items": items,
    }


def _quiz_section(db: Session, student_id: int) -> List[Dict[str, Any]]:
    attempts = db.query(QuizAttempt).filter(QuizAttempt.user_id == student_id).all()
    if not attempts:
        return []

    lesson_ids = {a.lesson_id for a in attempts if a.lesson_id}
    lesson_info = {
        lesson.id: {
            "title": (lesson.title or "").strip(),
            "order": (module.order_index or 0, lesson.order_index or 0),
        }
        for lesson, module in db.query(Lesson, Module)
        .join(Module, Module.id == Lesson.module_id)
        .filter(Lesson.id.in_(lesson_ids or [0])).all()
    }
    course_titles = {
        c.id: (c.title or "").strip()
        for c in db.query(Course).filter(
            Course.id.in_({a.course_id for a in attempts})
        ).all()
    }

    by_course: Dict[int, List[QuizAttempt]] = defaultdict(list)
    for a in attempts:
        by_course[a.course_id].append(a)

    courses = []
    for course_id, course_attempts in by_course.items():
        completed = [a for a in course_attempts if not a.is_draft]
        by_lesson: Dict[Optional[int], List[QuizAttempt]] = defaultdict(list)
        for a in completed:
            by_lesson[a.lesson_id].append(a)

        sections = []
        for lesson_id, lesson_attempts in by_lesson.items():
            info = lesson_info.get(lesson_id)
            lesson_attempts.sort(key=lambda a: a.completed_at or a.created_at or datetime.min)
            sections.append({
                "lesson_id": lesson_id,
                "lesson_title": info["title"] if info else f"Lesson {lesson_id}",
                "attempts": len(lesson_attempts),
                "average_pct": round(
                    sum(a.score_percentage for a in lesson_attempts) / len(lesson_attempts), 2
                ),
                "best_pct": round(max(a.score_percentage for a in lesson_attempts), 2),
                # Per-attempt drill-down for the report page.
                "attempt_details": [
                    {
                        "completed_at": _iso(a.completed_at),
                        "correct": a.correct_answers,
                        "total_questions": a.total_questions,
                        "pct": round(a.score_percentage, 2),
                    }
                    for a in lesson_attempts
                ],
                "_order": info["order"] if info else (10 ** 6, lesson_id or 0),
            })
        sections.sort(key=lambda s: s.pop("_order"))

        courses.append({
            "course_id": course_id,
            "course_title": course_titles.get(course_id, str(course_id)),
            "total_attempts": len(course_attempts),
            "completed_attempts": len(completed),
            "draft_attempts": len(course_attempts) - len(completed),
            "average_pct": round(
                sum(a.score_percentage for a in completed) / len(completed), 2
            ) if completed else None,
            "sections": sections,
        })
    courses.sort(key=lambda c: c["course_id"])
    return courses


def _attendance_section(db: Session, student_id: int) -> Dict[str, Any]:
    group_ids = [
        row[0] for row in db.query(GroupStudent.group_id).filter(
            GroupStudent.student_id == student_id
        ).all()
    ]
    empty = {
        "marked_total": 0, "attended": 0, "late": 0, "absent": 0,
        "attendance_pct": None, "absences": [], "lates": [],
    }
    if not group_ids:
        return empty

    events = (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_(group_ids),
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712 — SQLAlchemy comparison
        )
        .distinct()
        .all()
    )
    attendance = {
        a.event_id: a
        for a in db.query(Attendance).filter(
            Attendance.user_id == student_id,
            Attendance.event_id.in_([e.id for e in events] or [0]),
        ).all()
    }

    # A marked event dated in the future is a rescheduling artifact (attendance was
    # recorded, then the event's date was moved) — not a lesson that happened.
    horizon = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    marked = []
    for e in events:
        a = attendance.get(e.id)
        if not a or not a.status:
            continue
        if e.start_datetime and e.start_datetime > horizon:
            continue
        marked.append((e, a))
    marked.sort(key=lambda pair: pair[0].start_datetime or datetime.min)

    def _rows(status: str) -> List[Dict[str, Any]]:
        return [
            {"date": _iso(e.start_datetime), "title": e.title}
            for e, a in marked if a.status == status
        ]

    present = sum(1 for _, a in marked if a.status == "present")
    late = sum(1 for _, a in marked if a.status == "late")
    absent = sum(1 for _, a in marked if a.status == "absent")
    total = len(marked)
    return {
        "marked_total": total,
        "attended": present + late,
        "late": late,
        "absent": absent,
        "attendance_pct": round((present + late) / total * 100, 2) if total else None,
        "absences": _rows("absent"),
        "lates": _rows("late"),
    }


def build_student_report(db: Session, student_id: int) -> Dict[str, Any]:
    """Aggregate every result surface for one student into one dict."""
    student = _student_or_404(db, student_id)

    group_rows = (
        db.query(GroupStudent, Group)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(GroupStudent.student_id == student_id)
        .all()
    )

    az = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == student_id
    ).first()

    points = db.query(PointHistory).filter(PointHistory.user_id == student_id).all()
    points_by_reason: Dict[str, int] = defaultdict(int)
    for p in points:
        points_by_reason[p.reason] += p.amount

    return {
        "student": {
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "created_at": _iso(student.created_at),
            "groups": [
                {
                    "id": group.id,
                    "name": group.name,
                    "joined_at": _iso(getattr(link, "created_at", None)),
                }
                for link, group in group_rows
            ],
        },
        "homework": _homework_section(db, student_id),
        "quizzes": _quiz_section(db, student_id),
        "bluebook": [
            {
                "test_number": b.test_number,
                "taken_at": _iso(b.taken_at),
                "verbal": b.verbal_score,
                "math": b.math_score,
                "total": b.total_score,
                "source": b.source,
            }
            for b in db.query(BluebookResult)
            .filter(BluebookResult.student_id == student_id)
            .order_by(BluebookResult.test_number).all()
        ],
        "exams": {
            "results": [
                {
                    "exam_type": r.exam_type,
                    "test_date": _iso(r.test_date),
                    "total_score": float(r.total_score),
                    "verbal_score": r.verbal_score,
                    "math_score": r.math_score,
                    "status": r.status,
                }
                for r in db.query(ExamResult).filter(
                    ExamResult.student_id == student_id,
                    ExamResult.is_superseded == False,  # noqa: E712
                    ExamResult.status != "rejected",
                ).order_by(ExamResult.test_date).all()
            ],
            "sat_planned_date": _iso(az.sat_planned_test_date) if az else None,
            "ielts_planned_date": _iso(getattr(az, "ielts_planned_test_date", None)) if az else None,
        },
        "courses": [
            {
                "course_id": s.course_id,
                "course_title": (s.course.title or "").strip() if s.course else str(s.course_id),
                "total_steps": s.total_steps,
                "completed_steps": s.completed_steps,
                "completion_pct": round(s.completion_percentage or 0.0, 2),
                "time_spent_minutes": s.total_time_spent_minutes,
                "last_activity_at": _iso(s.last_activity_at),
            }
            for s in db.query(StudentCourseSummary).filter(
                StudentCourseSummary.user_id == student_id
            ).all()
        ],
        "attendance": _attendance_section(db, student_id),
        "activity": {
            "daily_questions_completed": db.query(DailyQuestionCompletion).filter(
                DailyQuestionCompletion.user_id == student_id
            ).count(),
            "points_total": sum(p.amount for p in points),
            "points_by_reason": dict(points_by_reason),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
