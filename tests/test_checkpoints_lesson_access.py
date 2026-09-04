"""Per-lesson and course-nav access to the SAT Checkpoints quizzes.

As of Task 1 the 9 checkpoint quiz lessons live in a "Checkpoints" module INSIDE the SAT course
itself (see scripts/seed_sat_checkpoints.py), not a separate hidden course. Two things guard a
locked checkpoint independently of each other:

- The per-lesson guard (`assert_student_may_view_checkpoint_lesson`) 403s a direct read of a
  checkpoint lesson/step/materials that isn't open for the student, regardless of what any
  listing shows — every checkpoint lesson is `is_initially_unlocked=True`, so without this a
  student holding Checkpoint 1 could read Checkpoints 2-9 (questions *and* `correct_answer`)
  straight off the lesson and step endpoints.
- Course-nav visibility (Task 2): a student in no checkpoints-enabled, active group sees no
  checkpoint lessons and no "Checkpoints" module at all — the feature is invisible outside the
  pilot. Inside an enabled group every checkpoint is listed, but a locked one carries no step
  content and is not `is_accessible`.
"""
import pytest
from fastapi import HTTPException
from typing import NamedTuple

from src.schemas.models import Module, Lesson, Step
from tests.checkpoint_fixtures import (
    make_user, make_group, enroll, make_sat_course, make_definition, _quiz_json,
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


def _make_checkpoints_module(db, course, n=2, n_questions=2):
    """A "Checkpoints" module inside `course`, mirroring the real seed's layout (see
    scripts/seed_sat_checkpoints.py's `_ensure_module`/`_ensure_quiz_lesson`)."""
    module = Module(title="Checkpoints", course_id=course.id, order_index=2)
    db.add(module); db.flush()
    lessons, steps = [], []
    for i in range(n):
        title = f"Checkpoint {i + 1}"
        l = Lesson(title=title, module_id=module.id, order_index=i, is_initially_unlocked=True)
        db.add(l); db.flush()
        s = Step(lesson_id=l.id, title="Quiz", content_type="quiz", order_index=0,
                 content_text=_quiz_json(title, n_questions))
        db.add(s); db.flush()
        lessons.append(l); steps.append(s)
    return module, lessons, steps


class _World(NamedTuple):
    """Supports both the old-style tuple unpacking (`admin, group, s, d1, d2, sat_course, lessons,
    steps = _world(db)`) and named access (`w.sat_course`, `w.cp1_lesson`, ...) for newer tests."""
    admin: object
    group: object
    student: object
    d1: object
    d2: object
    sat_course: object
    quiz_lessons: list
    quiz_steps: list

    @property
    def cp1_lesson(self):
        return self.quiz_lessons[0]

    @property
    def cp2_lesson(self):
        return self.quiz_lessons[1]


def _world(db):
    admin = make_user(db, role="admin")
    course, v, m = make_sat_course(db, n_verbal=4, n_math=2)
    _module, quiz_lessons, quiz_steps = _make_checkpoints_module(db, course, n=2)
    d1 = make_definition(db, course, 1, v[:2], m[0], quiz_lessons[0])
    d2 = make_definition(db, course, 2, v[2:4], m[1], quiz_lessons[1])
    group = make_group(db, enabled=True)
    s = make_user(db)
    enroll(db, s, group, course, admin)
    _open_cp1(db, admin, group, d1)   # CP1 open, CP2 stays locked
    return _World(admin, group, s, d1, d2, course, quiz_lessons, quiz_steps)


def _open_cp1(db, admin, group, d1):
    from src.checkpoints import service
    service.open_for_students(db, group=group, definition=d1, student_ids=None, actor_id=admin.id)


def test_open_checkpoint_lesson_is_readable(db):
    from src.courses.routes.courses import get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    assert len(get_lesson_steps(lessons[0].id, include_content=True, current_user=s, db=db)) == 1
    assert get_step(steps[0].id, current_user=s, db=db).id == steps[0].id


def test_other_checkpoint_lesson_is_forbidden(db):
    from src.courses.routes.courses import get_lesson, get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    for call in (
        lambda: get_lesson(lessons[1].id, current_user=s, db=db),
        lambda: get_lesson_steps(lessons[1].id, include_content=True, current_user=s, db=db),
        lambda: get_step(steps[1].id, current_user=s, db=db),
    ):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403


def test_other_checkpoint_lesson_materials_are_forbidden(db):
    from src.courses.routes.courses import get_lesson_materials
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    with pytest.raises(HTTPException) as e:
        get_lesson_materials(lessons[1].id, current_user=s, db=db)
    assert e.value.status_code == 403


def test_check_lesson_access_reports_the_locked_checkpoint(db):
    from src.courses.routes.courses import check_lesson_access
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    assert check_lesson_access(lessons[0].id, current_user=s, db=db)["accessible"] is True
    out = check_lesson_access(lessons[1].id, current_user=s, db=db)
    assert out["accessible"] is False and out["reason"]


def test_staff_can_read_every_checkpoint_lesson(db):
    from src.courses.routes.courses import get_lesson, get_lesson_steps, get_step
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    for lesson, step in zip(lessons, steps):
        assert get_lesson(lesson.id, current_user=admin, db=db).id == lesson.id
        assert len(get_lesson_steps(lesson.id, include_content=True, current_user=admin, db=db)) == 1
        assert get_step(step.id, current_user=admin, db=db).id == step.id


def test_non_checkpoint_lessons_are_untouched(db):
    """The guard must only ever fire on a checkpoint quiz lesson."""
    from src.courses.routes.courses import get_lesson_steps
    from src.checkpoints import service
    admin, group, s, d1, d2, _, lessons, steps = _world(db)
    sat_lesson_id = d2.required_units[0].lesson_id
    assert sat_lesson_id not in service.checkpoint_quiz_lesson_ids(db)
    assert get_lesson_steps(sat_lesson_id, include_content=True, current_user=s, db=db) is not None


def test_course_lesson_listing_strips_the_locked_checkpoint(db):
    """`GET /courses/{id}/lessons` lists every checkpoint of an enabled group's course, but a
    locked one carries no step content."""
    from src.courses.routes.courses import get_course_lessons
    admin, group, s, d1, d2, sat_course, lessons, steps = _world(db)
    out = get_course_lessons(sat_course.id, lightweight=False, current_user=s, db=db)
    by_id = {l.id: l for l in out}
    assert lessons[0].id in by_id and lessons[1].id in by_id
    assert [st.content_text for st in by_id[lessons[0].id].steps] == [steps[0].content_text]
    assert all(st.content_text is None for st in by_id[lessons[1].id].steps)


def test_module_lesson_listing_strips_the_locked_checkpoint(db):
    from src.courses.routes.courses import get_module_lessons
    admin, group, s, d1, d2, sat_course, lessons, steps = _world(db)
    out = get_module_lessons(sat_course.id, lessons[0].module_id, current_user=s, db=db)
    assert [l.id for l in out] == [lessons[0].id, lessons[1].id]
    by_id = {l.id: l for l in out}
    assert by_id[lessons[0].id].steps[0].content_text == steps[0].content_text
    assert all(st.content_text is None for st in by_id[lessons[1].id].steps)


def test_staff_see_every_checkpoint_lesson_in_listings(db):
    from src.courses.routes.courses import get_course_lessons, get_module_lessons
    admin, group, s, d1, d2, sat_course, lessons, steps = _world(db)
    all_ids = {l.id for l in get_course_lessons(sat_course.id, lightweight=False,
                                                current_user=admin, db=db)}
    assert {l.id for l in lessons} <= all_ids
    assert [l.id for l in get_module_lessons(sat_course.id, lessons[0].module_id,
                                             current_user=admin, db=db)] == [l.id for l in lessons]


def test_ordinary_course_listing_is_untouched_by_the_checkpoint_filter(db):
    """A course with no checkpoint quiz lessons must keep every lesson (and pay one cheap query)."""
    from src.courses.routes.courses import get_course_lessons
    from src.schemas.models import CourseGroupAccess
    admin, group, s, d1, d2, sat_course, lessons, steps = _world(db)
    other_course, ov, om = make_sat_course(db, n_verbal=2, n_math=1)
    # `s` is already a member of `group`; just grant that group access to the new course too.
    db.add(CourseGroupAccess(course_id=other_course.id, group_id=group.id,
                             granted_by=admin.id, is_active=True))
    db.flush()
    out = get_course_lessons(other_course.id, lightweight=False, current_user=s, db=db)
    assert len(out) == 3
    assert all(len(l.steps) == 2 for l in out)


def test_course_modules_hide_checkpoints_from_non_enabled_groups(db):
    from src.courses.routes.courses import get_course_modules
    w = _world(db)                      # existing helper in this file
    other_group = make_group(db, enabled=False, name="not-enabled")
    outsider = make_user(db)
    enroll(db, outsider, other_group, w.sat_course, w.admin)
    mods = get_course_modules(w.sat_course.id, include_lessons=True, student_id=None,
                              current_user=outsider, db=db)
    titles = [m["title"] if isinstance(m, dict) else m.title for m in mods]
    assert "Checkpoints" not in titles


def test_course_modules_show_locked_and_open_checkpoints_to_enabled_group(db):
    from src.courses.routes.courses import get_course_modules
    w = _world(db)                      # student has CP1 open, CP2 locked
    mods = get_course_modules(w.sat_course.id, include_lessons=True, student_id=None,
                              current_user=w.student, db=db)
    module = next(m for m in mods if (m["title"] if isinstance(m, dict) else m.title) == "Checkpoints")
    lessons = module["lessons"] if isinstance(module, dict) else module.lessons
    by_title = {l["title"]: l for l in lessons}
    assert by_title["Checkpoint 1"]["is_accessible"] is True
    assert by_title["Checkpoint 2"]["is_accessible"] is False
    # a locked checkpoint never ships its questions
    assert all(not s.get("content_text") for s in by_title["Checkpoint 2"].get("steps", []))
    assert (module["total_lessons"] if isinstance(module, dict) else module.total_lessons) == len(lessons)


def test_course_modules_without_lessons_hides_checkpoints_from_non_enabled_groups(db):
    """Finding 2 regression: the module must be omitted for a non-pilot student even when the
    caller asks for modules without lessons (include_lessons=False) — not just when it does."""
    from src.courses.routes.courses import get_course_modules
    w = _world(db)
    other_group = make_group(db, enabled=False, name="not-enabled-2")
    outsider = make_user(db)
    enroll(db, outsider, other_group, w.sat_course, w.admin)

    mods = get_course_modules(w.sat_course.id, include_lessons=False, student_id=None,
                              current_user=outsider, db=db)
    titles = [m["title"] if isinstance(m, dict) else m.title for m in mods]
    assert "Checkpoints" not in titles

    mods_enabled = get_course_modules(w.sat_course.id, include_lessons=False, student_id=None,
                                      current_user=w.student, db=db)
    titles_enabled = [m["title"] if isinstance(m, dict) else m.title for m in mods_enabled]
    assert "Checkpoints" in titles_enabled


def test_course_modules_locked_checkpoint_carries_no_step_content(db):
    """Finding 1 regression guard: get_course_modules(include_lessons=True) never lazy-loads a
    locked checkpoint's real steps — its rendered payload must still carry no content_text."""
    from src.courses.routes.courses import get_course_modules
    w = _world(db)                      # student has CP1 open, CP2 locked
    mods = get_course_modules(w.sat_course.id, include_lessons=True, student_id=None,
                              current_user=w.student, db=db)
    module = next(m for m in mods if (m["title"] if isinstance(m, dict) else m.title) == "Checkpoints")
    lessons = module["lessons"] if isinstance(module, dict) else module.lessons
    by_title = {l["title"]: l for l in lessons}
    locked_steps = by_title["Checkpoint 2"].get("steps", [])
    assert locked_steps, "expected the locked checkpoint to still list its step metadata"
    assert all(s.get("content_text") is None for s in locked_steps)


def test_check_lesson_access_reports_the_locked_reason(db):
    from src.courses.routes.courses import check_lesson_access
    w = _world(db)
    ok = check_lesson_access(w.cp1_lesson.id, current_user=w.student, db=db)
    assert ok["accessible"] is True
    blocked = check_lesson_access(w.cp2_lesson.id, current_user=w.student, db=db)
    assert blocked["accessible"] is False and "Unit" in blocked["reason"]
