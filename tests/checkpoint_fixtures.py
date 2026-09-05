"""Factories shared by the checkpoint test files (house style: each test file keeps its own `db` fixture)."""
import itertools
import json
import uuid
from datetime import datetime, timezone

from src.schemas.models import (
    UserInDB, Group, GroupStudent, Course, Module, Lesson, Step, CourseGroupAccess,
    StudentProgress, StepProgress,
)
from src.utils.auth_utils import hash_password


_seq = itertools.count(1)


def make_user(db, role="student", email=None, name=None):
    email = email or f"{role}-{next(_seq)}-{uuid.uuid4().hex[:8]}@cp.test"
    u = UserInDB(email=email, name=name or email.split("@")[0], role=role,
                 hashed_password=hash_password("x"))
    db.add(u); db.flush()
    return u


def make_group(db, *, program_type="sat", enabled=False, start_number=1, name="cp-grp"):
    g = Group(name=name, program_type=program_type, checkpoints_enabled=enabled,
              checkpoints_start_number=start_number, is_active=True)
    db.add(g); db.flush()
    return g


def enroll(db, student, group, course, granted_by):
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.add(CourseGroupAccess(course_id=course.id, group_id=group.id,
                             granted_by=granted_by.id, is_active=True))
    db.flush()


def make_sat_course(db, n_verbal=4, n_math=2):
    """Course with modules 'Verbal'/'Math'; each lesson has 2 required text steps."""
    c = Course(title="SAT test course", course_type="sat", is_active=True)
    db.add(c); db.flush()
    verbal = Module(title="Verbal", course_id=c.id, order_index=0)
    math = Module(title="Math", course_id=c.id, order_index=1)
    db.add_all([verbal, math]); db.flush()
    v_lessons, m_lessons = [], []
    for i in range(1, n_verbal + 1):
        l = Lesson(title=f"Unit {i}: Verbal", module_id=verbal.id, order_index=i)
        db.add(l); db.flush()
        db.add_all([Step(lesson_id=l.id, title=f"S{k}", content_type="text", order_index=k) for k in range(2)])
        v_lessons.append(l)
    for i in range(1, n_math + 1):
        l = Lesson(title=f"Unit {i}: Math", module_id=math.id, order_index=i)
        db.add(l); db.flush()
        db.add_all([Step(lesson_id=l.id, title=f"S{k}", content_type="text", order_index=k) for k in range(2)])
        m_lessons.append(l)
    db.flush()
    return c, v_lessons, m_lessons


def _quiz_json(title, n_questions):
    questions = [{
        "id": f"q{i}", "assignment_id": "", "question_text": f"Q{i}", "question_type": "single_choice",
        "options": [{"id": f"q{i}a", "text": "A", "is_correct": True, "letter": "A"},
                    {"id": f"q{i}b", "text": "B", "is_correct": False, "letter": "B"}],
        "correct_answer": 0, "points": 1, "order_index": i, "difficulty": "easy",
    } for i in range(n_questions)]
    return json.dumps({"title": title, "questions": questions})


def make_quiz_lessons(db, course=None, n=2, n_questions=2):
    """Quiz lessons in a "Checkpoints" module. Returns (course, [lessons], [steps]).

    Every lesson is `is_initially_unlocked=True`, like the real seed — which is exactly why the
    per-lesson checkpoint guard is needed on top of the course-level access hook.

    With `course` given, the "Checkpoints" module is created (or reused, if one already exists)
    inside that course — this is the real seed's shape, where checkpoint quizzes live inside the
    SAT course itself. With `course` omitted, the old pre-move shape is kept exactly: a separate
    hidden "SAT Checkpoints (test)" course is created to hold them, so existing callers are
    unaffected.
    """
    if course is None:
        c = Course(title="SAT Checkpoints (test)", course_type="sat", is_active=True)
        db.add(c); db.flush()
    else:
        c = course
    m = db.query(Module).filter(Module.course_id == c.id, Module.title == "Checkpoints").first()
    if m is None:
        order_index = db.query(Module).filter(Module.course_id == c.id).count()
        m = Module(title="Checkpoints", course_id=c.id, order_index=order_index)
        db.add(m); db.flush()
    lessons, steps = [], []
    for i in range(n):
        title = f"Checkpoint {i + 1}"
        l = Lesson(title=title, module_id=m.id, order_index=i, is_initially_unlocked=True)
        db.add(l); db.flush()
        s = Step(lesson_id=l.id, title="Quiz", content_type="quiz", order_index=0,
                 content_text=_quiz_json(title, n_questions))
        db.add(s); db.flush()
        lessons.append(l); steps.append(s)
    return c, lessons, steps


def make_quiz_lesson(db, title="Checkpoint 1", n_questions=2):
    """Hidden 'SAT Checkpoints' course with one lesson + one quiz step. Returns (course, lesson, step)."""
    c = Course(title="SAT Checkpoints (test)", course_type="sat", is_active=True)
    db.add(c); db.flush()
    m = Module(title="Checkpoints", course_id=c.id, order_index=0)
    db.add(m); db.flush()
    l = Lesson(title=title, module_id=m.id, order_index=0, is_initially_unlocked=True)
    db.add(l); db.flush()
    s = Step(lesson_id=l.id, title="Quiz", content_type="quiz", order_index=0,
             content_text=_quiz_json(title, n_questions))
    db.add(s); db.flush()
    return c, l, s


def make_definition(db, course, number, verbal_lessons, math_lesson, quiz_lesson=None, is_active=True):
    from src.checkpoints.models import CheckpointDefinition, CheckpointRequiredUnit
    d = CheckpointDefinition(course_id=course.id, number=number, title=f"Checkpoint {number}",
                             quiz_lesson_id=quiz_lesson.id if quiz_lesson else None,
                             total_questions=45, is_active=is_active)
    db.add(d); db.flush()
    pos = 0
    for l in verbal_lessons:
        db.add(CheckpointRequiredUnit(checkpoint_id=d.id, lesson_id=l.id, kind="verbal", position=pos)); pos += 1
    db.add(CheckpointRequiredUnit(checkpoint_id=d.id, lesson_id=math_lesson.id, kind="math", position=pos))
    db.flush()
    db.refresh(d)
    return d


def complete_lesson_explicit(db, student, course, lesson):
    db.add(StudentProgress(user_id=student.id, course_id=course.id, lesson_id=lesson.id,
                           status="completed", completion_percentage=100,
                           completed_at=datetime.now(timezone.utc)))
    db.flush()


def complete_lesson_via_steps(db, student, course, lesson):
    for s in db.query(Step).filter(Step.lesson_id == lesson.id).all():
        db.add(StepProgress(user_id=student.id, course_id=course.id, lesson_id=lesson.id,
                            step_id=s.id, status="completed",
                            completed_at=datetime.now(timezone.utc)))
    db.flush()


def complete_checkpoint(db, student, definition, *, correct=40, total=45):
    """Submit a passing quiz attempt for `definition` and record it onto the student's row(s),
    flipping their status to 'completed' — i.e. this checkpoint is now "cleared" per the ordinal
    blocking rule."""
    from src.checkpoints import service
    from src.schemas.models import QuizAttempt, Step, Lesson, Module
    quiz_step = db.query(Step).filter(Step.lesson_id == definition.quiz_lesson_id).first()
    quiz_course_id = db.query(Module.course_id).join(
        Lesson, Lesson.module_id == Module.id).filter(Lesson.id == definition.quiz_lesson_id).scalar()
    attempt = QuizAttempt(user_id=student.id, step_id=quiz_step.id, course_id=quiz_course_id,
                          lesson_id=definition.quiz_lesson_id, total_questions=total,
                          correct_answers=correct, score_percentage=round(100 * correct / total, 2),
                          is_draft=False, completed_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(attempt); db.flush()
    service.record_submission(db, student.id, attempt)
    return attempt
