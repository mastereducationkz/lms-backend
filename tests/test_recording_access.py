"""Who may watch a lesson recording.

This is the security surface of the whole feature. A lesson recording shows a classroom
of identifiable students, so the access matrix from spec §4.4 and §4.9 is pinned
explicitly rather than left to whatever the query happens to do:

* a student in the lesson's group may watch **whether or not they attended** — rewatching
  a missed lesson is the entire point, so attendance must never become a condition;
* a student in a different group may not, and gets 404 rather than 403, because 403 would
  confirm the recording exists;
* the lesson's teacher may watch;
* curators, head teachers and admins may watch for quality review (§4.9).

Also pinned: the playback URL is minted per viewer, so this response must never be cached.
"""
import pytest

from src.events.routes import recordings as recordings_route


class _User:
    def __init__(self, id, role):
        self.id = id
        self.role = role


class _Event:
    def __init__(self, teacher_id=500):
        self.id = 10
        self.teacher_id = teacher_id


class _MembershipQuery:
    """Stands in for the student-in-group lookup."""

    def __init__(self, member):
        self._member = member

    def join(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return object() if self._member else None


class _DB:
    def __init__(self, member=False):
        self._member = member
        self.queried = []

    def query(self, *a):
        self.queried.extend(a)
        return _MembershipQuery(self._member)


# --- students ----------------------------------------------------------------

def test_student_in_the_group_may_watch():
    assert recordings_route._may_watch(_DB(member=True), _User(1, "student"), _Event()) is True


def test_student_in_another_group_may_not():
    assert recordings_route._may_watch(_DB(member=False), _User(2, "student"), _Event()) is False


def test_attendance_is_not_a_condition():
    """A student who missed the lesson still watches — that is the feature's main purpose.

    Asserted behaviourally, by checking which models the access decision consults: the
    Attendance table must never be one of them. Guards against someone later
    "tightening" this into an attended-only rule (spec 4.4).
    """
    from src.schemas.models import Attendance

    db = _DB(member=True)
    assert recordings_route._may_watch(db, _User(1, "student"), _Event()) is True
    assert Attendance not in db.queried, "access must not depend on having attended"
    assert db.queried, "membership really is looked up"


# --- staff -------------------------------------------------------------------

def test_the_lessons_teacher_may_watch():
    assert recordings_route._may_watch(_DB(), _User(500, "teacher"), _Event(teacher_id=500)) is True


@pytest.mark.parametrize("role", ["curator", "head_curator", "head_teacher", "admin"])
def test_reviewers_may_watch(role):
    """Quality review, disclosed to teachers in writing before launch (spec 4.9)."""
    assert recordings_route._may_watch(_DB(), _User(9, role), _Event()) is True


def test_a_teacher_from_another_group_may_watch_by_role():
    """Teachers are reviewers too; this is intentional and disclosed, not an oversight."""
    assert recordings_route._may_watch(_DB(), _User(501, "teacher"), _Event(teacher_id=500)) is True


# --- caching -----------------------------------------------------------------

def test_endpoint_is_not_cached():
    """The URL carries a per-viewer token; caching would leak it to every other viewer.

    The same trap was hit once already on get_lesson_steps, whose @cached key omits the
    user. Pin it so nobody 'optimises' this endpoint later.
    """
    import inspect

    source = inspect.getsource(recordings_route)
    assert "@cached" not in source
    assert "cached(" not in source
