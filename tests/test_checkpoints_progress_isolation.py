import json

import pytest

from src.schemas.models import Lesson, Module, Step
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_definition, complete_lesson_explicit,
)


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


def _add_checkpoint_lesson(db, course, number=1):
    module = db.query(Module).filter(Module.course_id == course.id,
                                     Module.title == "Checkpoints").first()
    if module is None:
        module = Module(title="Checkpoints", course_id=course.id, order_index=2)
        db.add(module); db.flush()
    lesson = Lesson(title=f"Checkpoint {number}", module_id=module.id, order_index=number - 1,
                    is_initially_unlocked=True, kind="checkpoint")
    db.add(lesson); db.flush()
    step = Step(lesson_id=lesson.id, title="Quiz", content_type="quiz", order_index=0,
                content_text=json.dumps({"title": f"Checkpoint {number}", "questions": []}))
    db.add(step); db.flush()
    return lesson, step


def test_checkpoint_lessons_do_not_change_course_progress(db):
    from src.progress.services.lesson_completion import calculate_student_module_progress
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    student = make_user(db)
    group = make_group(db, enabled=True)
    enroll(db, student, group, course, admin)
    complete_lesson_explicit(db, student, course, v[0])

    before = calculate_student_module_progress(db, student.id, course.id)
    cp_lesson, _ = _add_checkpoint_lesson(db, course, 1)
    make_definition(db, course, 1, v[:2], m[0], cp_lesson)
    after = calculate_student_module_progress(db, student.id, course.id)

    assert after["total_modules"] == before["total_modules"]
    assert after["overall_progress"] == before["overall_progress"]
    assert after["current_module_id"] == before["current_module_id"]


def test_batched_progress_matches_single(db):
    from src.progress.services.lesson_completion import (
        calculate_student_module_progress, calculate_module_progress_for_students,
    )
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    student = make_user(db)
    group = make_group(db, enabled=True)
    enroll(db, student, group, course, admin)
    cp_lesson, _ = _add_checkpoint_lesson(db, course, 1)
    make_definition(db, course, 1, v[:2], m[0], cp_lesson)

    batched = calculate_module_progress_for_students(db, [student.id], course.id)
    single = calculate_student_module_progress(db, student.id, course.id)
    assert batched[student.id]["total_modules"] == single["total_modules"]
    assert batched[student.id]["overall_progress"] == single["overall_progress"]


def test_staff_summaries_list_checkpoints_as_flagged_rows(db):
    from src.progress.services.lesson_completion import (
        get_user_lesson_progress_summary, get_group_lesson_progress_summary,
    )
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    student = make_user(db)
    group = make_group(db, enabled=True)
    enroll(db, student, group, course, admin)
    complete_lesson_explicit(db, student, course, v[0])
    cp_lesson, _ = _add_checkpoint_lesson(db, course, 1)
    make_definition(db, course, 1, v[:2], m[0], cp_lesson)

    user_summary = get_user_lesson_progress_summary(db, student.id, course.id)
    rows = {l["lesson_id"]: l for l in user_summary["lessons"]}
    assert cp_lesson.id in rows and rows[cp_lesson.id]["kind"] == "checkpoint"
    assert rows[v[0].id]["kind"] == "unit"
    # ...but the checkpoint's steps are not folded into the aggregate
    unit_steps = sum(l["total_steps"] for l in user_summary["lessons"] if l["kind"] == "unit")
    assert user_summary["overall"]["total_steps"] == unit_steps

    group_summary = get_group_lesson_progress_summary(db, group.id, course.id)
    grows = {l["lesson_id"]: l for l in group_summary["lessons"]}
    assert grows[cp_lesson.id]["kind"] == "checkpoint"


def test_checkpoint_submission_awards_no_points(db):
    from src.progress.routes.progress import _maybe_award_course_quiz_points
    from src.schemas.models import PointHistory
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=2, n_math=1)
    student = make_user(db)
    group = make_group(db, enabled=True)
    enroll(db, student, group, course, admin)
    cp_lesson, cp_step = _add_checkpoint_lesson(db, course, 1)
    make_definition(db, course, 1, v[:2], m[0], cp_lesson)

    _maybe_award_course_quiz_points(db, user_id=student.id, step_id=cp_step.id,
                                    course_id=course.id, score_percentage=95.0,
                                    is_graded=True, attempt_id=1)
    db.flush()
    awarded = db.query(PointHistory).filter(PointHistory.user_id == student.id,
                                            PointHistory.reason == "course_quiz").count()
    assert awarded == 0
