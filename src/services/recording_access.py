"""Who may watch a lesson recording — one rule, asked by playback, the library and the calendar.

A recording shows a classroom of identifiable students, so the rule is written once here and
every surface imports it. Before 2026-09-10 the playback check let *any* teacher or curator
watch *any* lesson, while the Lesson Recordings page promised "only the group and its
teacher". The owner chose the promise ("own lessons only"):

* admins, head curators and head teachers — every recording (oversight);
* a teacher — lessons they taught, which covers substitutions, and lessons of groups they own,
  so a lesson somebody covered for them still appears in their own history;
* a curator — lessons of groups they curate;
* a student — lessons of groups they belong to, **whether or not they attended**: rewatching a
  missed lesson is the point;
* anyone else, parents included — nothing.

Callers deny with 404, never 403: a 403 would confirm that a recording exists.

The clause is correlated to ``events`` alone, over its own aliases, for the reason
``operational_groups.event_has_operational_group_clause`` documents: LMS event queries often
join ``event_groups`` or ``users`` for their own purposes, and an auto-correlated subquery
would silently bind to *that* row.
"""
from __future__ import annotations

from sqlalchemy import and_, exists, false, or_, true
from sqlalchemy.orm import aliased

ALL_RECORDINGS_ROLES = frozenset({"admin", "head_curator", "head_teacher"})


def sees_every_recording(user) -> bool:
    return getattr(user, "role", None) in ALL_RECORDINGS_ROLES


def watchable_event_clause(user):
    """SQL: the events whose recording ``user`` may watch."""
    from src.schemas.models import Event, EventGroup, Group, GroupStudent

    role = getattr(user, "role", None)
    uid = getattr(user, "id", None)
    if role in ALL_RECORDINGS_ROLES:
        return true()
    if uid is None:
        return false()

    link, group = aliased(EventGroup), aliased(Group)

    def lesson_of_a_group_where(condition):
        return (
            exists()
            .where(and_(link.event_id == Event.id, link.group_id == group.id, condition))
            .correlate(Event)
        )

    if role == "teacher":
        return or_(Event.teacher_id == uid, lesson_of_a_group_where(group.teacher_id == uid))
    if role == "curator":
        return lesson_of_a_group_where(group.curator_id == uid)
    if role == "student":
        member = aliased(GroupStudent)
        return (
            exists()
            .where(and_(link.event_id == Event.id, link.group_id == member.group_id,
                        member.student_id == uid))
            .correlate(Event)
        )
    return false()


def may_watch(db, user, event) -> bool:
    """True if ``user`` may watch the recording of ``event``."""
    from src.schemas.models import Event

    if sees_every_recording(user):
        return True
    return (
        db.query(Event.id)
        .filter(Event.id == event.id, watchable_event_clause(user))
        .first()
        is not None
    )


def public_status(recording) -> str:
    """The API's vocabulary: ready | pending | failed | removed (ready, but video retired)."""
    if recording.status == "ready" and not recording.hls_url:
        return "removed"
    return recording.status
