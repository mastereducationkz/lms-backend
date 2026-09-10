"""Who may watch a lesson recording — the security surface of the whole feature.

A recording shows a classroom of identifiable students. Until 2026-09-10 any teacher or
curator could watch any lesson, while the Lesson Recordings page promised "only the group and
its teacher". The owner chose the promise ("own lessons only"); this matrix pins it, against a
real database, for every role and every way a person can be related to a lesson:

* students of the lesson's group — yes, **attended or not** (rewatching a missed lesson is the
  point); students of another group — no, and 404 rather than 403;
* the teacher who taught it (substitutions included) and the teacher who owns the group — yes;
  any other teacher — no;
* the group's curator — yes; another curator — no;
* head curators, head teachers, admins — every recording; parents — none.

Also pinned: the playback URL is minted per viewer, so neither endpoint may ever be cached.
"""
import inspect

import pytest

from src.events.routes import recording_library, recordings as recordings_route
from src.services.recording_access import may_watch, watchable_event_clause
from src.schemas.models import Event
from tests.test_operational_groups import _user, db, world  # noqa: F401 - fixtures


@pytest.fixture
def lesson(world):
    """A taught lesson of group G: owner teacher T, curator C, student S (did not attend)."""
    db = world["db"]
    curator = _user(db, "curator")
    group = world["group"](name="July 8 SAT - Gulzada", curator_id=curator.id)
    student = world["enrol"](group)
    ev = world["lesson"](group, days_ahead=-1)
    return {"db": db, "event": ev, "group": group, "owner": world["teacher"],
            "curator": curator, "student": student, "world": world}


def _watch(lesson, user):
    return may_watch(lesson["db"], user, lesson["event"])


def test_a_student_of_the_group_may_watch_even_if_absent(lesson):
    assert _watch(lesson, lesson["student"]) is True


def test_a_student_of_another_group_may_not(lesson):
    other = lesson["world"]["group"](name="other")
    outsider = lesson["world"]["enrol"](other)
    assert _watch(lesson, outsider) is False


def test_the_groups_own_teacher_may_watch(lesson):
    assert _watch(lesson, lesson["owner"]) is True


def test_a_substitute_may_watch_the_lesson_they_taught(lesson):
    sub = _user(lesson["db"], "teacher")
    lesson["event"].teacher_id = sub.id
    lesson["db"].flush()
    assert _watch(lesson, sub) is True
    assert _watch(lesson, lesson["owner"]) is True, "the owner keeps a covered lesson in their history"


def test_any_other_teacher_may_not(lesson):
    """The change of 2026-09-10: 'teacher' is no longer a pass to every classroom."""
    assert _watch(lesson, _user(lesson["db"], "teacher")) is False


def test_the_groups_curator_may_watch(lesson):
    assert _watch(lesson, lesson["curator"]) is True


def test_another_curator_may_not(lesson):
    assert _watch(lesson, _user(lesson["db"], "curator")) is False


@pytest.mark.parametrize("role", ["admin", "head_curator", "head_teacher"])
def test_oversight_roles_see_every_recording(lesson, role):
    assert _watch(lesson, _user(lesson["db"], role)) is True


def test_parents_and_unknown_roles_see_nothing(lesson):
    assert _watch(lesson, _user(lesson["db"], "parent")) is False
    assert _watch(lesson, _user(lesson["db"], "somebody")) is False


def test_the_clause_and_the_check_agree(lesson):
    """The library and the calendar list with the clause; playback checks with may_watch."""
    db = lesson["db"]
    for user in (lesson["student"], lesson["owner"], lesson["curator"], _user(db, "teacher")):
        listed = db.query(Event.id).filter(Event.id == lesson["event"].id,
                                           watchable_event_clause(user)).first() is not None
        assert listed == _watch(lesson, user), user.role


def test_a_denial_is_a_404_not_a_403(lesson):
    from fastapi import HTTPException

    outsider = _user(lesson["db"], "teacher")
    with pytest.raises(HTTPException) as denied:
        recordings_route.get_lesson_recording(event_id=lesson["event"].id, db=lesson["db"],
                                              current_user=outsider)
    assert denied.value.status_code == 404


@pytest.mark.parametrize("module", [recordings_route, recording_library])
def test_neither_endpoint_is_cached(module):
    """URLs carry a per-viewer token; caching would leak it to every other viewer.

    The same trap was hit once already on get_lesson_steps, whose @cached key omits the user.
    """
    source = inspect.getsource(module)
    assert "@cached" not in source
    assert "cached(" not in source
