import pytest

from tests.checkpoint_fixtures import make_user, make_sat_course, complete_lesson_explicit, complete_lesson_via_steps


@pytest.fixture
def db():
    # join_transaction_mode="create_savepoint" (SQLAlchemy 2.0) instead of the older
    # begin_nested()+after_transaction_end-listener recipe: that recipe rebuilds its savepoint by
    # reacting to the transaction the app's own db.commit() just tore down, so an app-level
    # db.rollback() straight after a commit unwinds past it and takes committed rows with it.
    # create_savepoint keeps every app-level commit/rollback nested one level down, inside a
    # connection-level transaction this fixture always rolls back. See tests/onboarding_fixtures.py.
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


def test_explicit_and_step_based_completion_both_count(db):
    from src.checkpoints.completion import completed_lesson_ids
    course, v, m = make_sat_course(db)
    s = make_user(db)
    complete_lesson_explicit(db, s, course, v[0])
    complete_lesson_via_steps(db, s, course, m[0])
    ids = [v[0].id, v[1].id, m[0].id]
    assert completed_lesson_ids(db, s.id, ids) == {v[0].id, m[0].id}


def test_partial_steps_do_not_count(db):
    from src.checkpoints.completion import completed_lesson_ids
    from src.schemas.models import Step, StepProgress
    course, v, m = make_sat_course(db)
    s = make_user(db)
    first_step = db.query(Step).filter(Step.lesson_id == v[0].id).order_by(Step.order_index).first()
    db.add(StepProgress(user_id=s.id, course_id=course.id, lesson_id=v[0].id,
                        step_id=first_step.id, status="completed")); db.flush()
    assert completed_lesson_ids(db, s.id, [v[0].id]) == set()


def test_empty_input(db):
    from src.checkpoints.completion import completed_lesson_ids
    s = make_user(db)
    assert completed_lesson_ids(db, s.id, []) == set()


# --- completed_lesson_counts (bulk sibling of completed_lesson_ids) ----------

def test_bulk_counts_explicit_lesson_row(db):
    from src.checkpoints.completion import completed_lesson_counts
    course, v, m = make_sat_course(db)
    s = make_user(db)
    complete_lesson_explicit(db, s, course, v[0])
    counts = completed_lesson_counts(db, [s.id], [v[0].id])
    assert counts == {v[0].id: 1}


def test_bulk_counts_all_required_steps_with_no_lesson_row(db):
    from src.checkpoints.completion import completed_lesson_counts
    course, v, m = make_sat_course(db)
    s = make_user(db)
    complete_lesson_via_steps(db, s, course, v[0])
    counts = completed_lesson_counts(db, [s.id], [v[0].id])
    assert counts == {v[0].id: 1}


def test_bulk_partial_steps_do_not_count(db):
    from src.checkpoints.completion import completed_lesson_counts
    from src.schemas.models import Step, StepProgress
    course, v, m = make_sat_course(db)
    s = make_user(db)
    first_step = db.query(Step).filter(Step.lesson_id == v[0].id).order_by(Step.order_index).first()
    db.add(StepProgress(user_id=s.id, course_id=course.id, lesson_id=v[0].id,
                        step_id=first_step.id, status="completed")); db.flush()
    counts = completed_lesson_counts(db, [s.id], [v[0].id])
    assert counts.get(v[0].id, 0) == 0


def test_bulk_incomplete_optional_step_does_not_block_completion(db):
    from src.checkpoints.completion import completed_lesson_counts
    from src.schemas.models import Course, Module, Lesson, Step, StepProgress
    course = Course(title="Optional-step course", is_active=True); db.add(course); db.flush()
    module = Module(title="M", course_id=course.id, order_index=0); db.add(module); db.flush()
    lesson = Lesson(title="Unit", module_id=module.id, order_index=0); db.add(lesson); db.flush()
    required_step = Step(lesson_id=lesson.id, title="Required", content_type="text",
                         order_index=0, is_optional=False)
    optional_step = Step(lesson_id=lesson.id, title="Optional", content_type="text",
                         order_index=1, is_optional=True)
    db.add_all([required_step, optional_step]); db.flush()
    s = make_user(db)
    db.add(StepProgress(user_id=s.id, course_id=course.id, lesson_id=lesson.id,
                        step_id=required_step.id, status="completed")); db.flush()
    counts = completed_lesson_counts(db, [s.id], [lesson.id])
    assert counts == {lesson.id: 1}


def test_bulk_all_optional_steps_completing_all_counts(db):
    from src.checkpoints.completion import completed_lesson_counts
    from src.schemas.models import Course, Module, Lesson, Step, StepProgress
    course = Course(title="All-optional course", is_active=True); db.add(course); db.flush()
    module = Module(title="M", course_id=course.id, order_index=0); db.add(module); db.flush()
    lesson = Lesson(title="Unit", module_id=module.id, order_index=0); db.add(lesson); db.flush()
    step_a = Step(lesson_id=lesson.id, title="A", content_type="text", order_index=0, is_optional=True)
    step_b = Step(lesson_id=lesson.id, title="B", content_type="text", order_index=1, is_optional=True)
    db.add_all([step_a, step_b]); db.flush()
    s = make_user(db)
    db.add_all([
        StepProgress(user_id=s.id, course_id=course.id, lesson_id=lesson.id,
                    step_id=step_a.id, status="completed"),
        StepProgress(user_id=s.id, course_id=course.id, lesson_id=lesson.id,
                    step_id=step_b.id, status="completed"),
    ])
    db.flush()
    counts = completed_lesson_counts(db, [s.id], [lesson.id])
    assert counts == {lesson.id: 1}


def test_bulk_only_given_users_are_counted(db):
    from src.checkpoints.completion import completed_lesson_counts
    course, v, m = make_sat_course(db)
    inside = make_user(db)
    outside = make_user(db)
    complete_lesson_explicit(db, inside, course, v[0])
    complete_lesson_explicit(db, outside, course, v[0])
    counts = completed_lesson_counts(db, [inside.id], [v[0].id])
    assert counts == {v[0].id: 1}


def test_bulk_empty_inputs(db):
    from src.checkpoints.completion import completed_lesson_counts
    s = make_user(db)
    assert completed_lesson_counts(db, [], [1, 2]) == {}
    assert completed_lesson_counts(db, [s.id], []) == {}


def test_zero_step_lesson_never_counts_as_completed(db):
    """A lesson with no Step rows at all must not count as completed for anyone, for
    either function."""
    from src.checkpoints.completion import completed_lesson_ids, completed_lesson_counts
    from src.schemas.models import Course, Module, Lesson
    course = Course(title="No-step course", is_active=True); db.add(course); db.flush()
    module = Module(title="M", course_id=course.id, order_index=0); db.add(module); db.flush()
    lesson = Lesson(title="Empty unit", module_id=module.id, order_index=0); db.add(lesson); db.flush()
    s = make_user(db)
    assert completed_lesson_ids(db, s.id, [lesson.id]) == set()
    assert completed_lesson_counts(db, [s.id], [lesson.id]) == {}


def test_duplicate_ids_do_not_double_count(db):
    """Duplicate ids in user_ids/lesson_ids must not inflate results for either function."""
    from src.checkpoints.completion import completed_lesson_ids, completed_lesson_counts
    course, v, m = make_sat_course(db)
    s = make_user(db)
    complete_lesson_explicit(db, s, course, v[0])

    assert completed_lesson_ids(db, s.id, [v[0].id, v[0].id]) == {v[0].id}

    counts = completed_lesson_counts(db, [s.id, s.id], [v[0].id, v[0].id])
    assert counts == {v[0].id: 1}


def test_bulk_agrees_with_per_user_definition_across_mixed_fixture(db):
    """Property-style guard: completed_lesson_counts must exactly agree, per user and per
    lesson, with completed_lesson_ids — the two must never be allowed to drift apart."""
    from src.checkpoints.completion import completed_lesson_counts, completed_lesson_ids
    course, v, m = make_sat_course(db, n_verbal=3, n_math=2)
    lessons = v + m

    explicit_student = make_user(db)
    complete_lesson_explicit(db, explicit_student, course, v[0])
    complete_lesson_via_steps(db, explicit_student, course, m[0])

    steps_student = make_user(db)
    complete_lesson_via_steps(db, steps_student, course, v[1])

    partial_student = make_user(db)
    from src.schemas.models import Step, StepProgress
    first_step = db.query(Step).filter(Step.lesson_id == v[2].id).order_by(Step.order_index).first()
    db.add(StepProgress(user_id=partial_student.id, course_id=course.id, lesson_id=v[2].id,
                        step_id=first_step.id, status="completed"))
    db.flush()

    untouched_student = make_user(db)

    students = [explicit_student, steps_student, partial_student, untouched_student]
    lesson_ids = [l.id for l in lessons]

    bulk_counts = completed_lesson_counts(db, [s.id for s in students], lesson_ids)

    for lid in lesson_ids:
        expected_completers = {
            s.id for s in students if lid in completed_lesson_ids(db, s.id, lesson_ids)
        }
        actual = bulk_counts.get(lid, 0)
        assert actual == len(expected_completers), (
            f"lesson {lid}: bulk={actual} vs per-user={len(expected_completers)}"
        )
