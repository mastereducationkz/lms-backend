"""A student studying two products at once — the world the scoped freeze exists for.

Shared by the two scoped-freeze test modules rather than duplicated, following the same
pattern as :mod:`tests.onboarding_fixtures`: one student in a SAT group and an IELTS group,
so every test can say "freeze this one, leave that one running".
"""
from datetime import date, datetime, timedelta

import pytest

from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, UserInDB
from tests.onboarding_fixtures import db  # noqa: F401 - re-exported for importers


@pytest.fixture
def world(db):  # noqa: F811
    curator = UserInDB(
        email="cur@lms.local", name="Куратор", role="curator", is_active=True,
        hashed_password="x",
    )
    student = UserInDB(
        email="stu@lms.local", name="Ученик", role="student", is_active=True,
        last_activity_date=date.today(), hashed_password="x",
    )
    db.add_all([curator, student])
    db.flush()
    groups = {}
    for name, program in (("SAT-1", "SAT"), ("IELTS-1", "IELTS")):
        group = Group(
            name=name, curator_id=curator.id, is_active=True, is_over=False,
            program_type=program, group_type="group",
        )
        db.add(group)
        db.flush()
        db.add(
            GroupStudent(
                group_id=group.id,
                student_id=student.id,
                created_at=datetime.utcnow() - timedelta(days=60),
            )
        )
        groups[program] = group
    db.commit()
    return {"db": db, "curator": curator, "student": student, "groups": groups}


def lesson(world, group, *, days_ago: float, status=None):
    """One past lesson of ``group``, optionally already marked for the student."""
    db = world["db"]
    start = datetime.utcnow() - timedelta(days=days_ago)
    event = Event(
        title="Урок", event_type="class", start_datetime=start,
        end_datetime=start + timedelta(hours=1), is_active=True,
        teacher_id=None, created_by=world["curator"].id,
    )
    db.add(event)
    db.flush()
    db.add(EventGroup(event_id=event.id, group_id=group.id))
    if status is not None:
        db.add(Attendance(event_id=event.id, user_id=world["student"].id, status=status))
    db.commit()
    return event


def freeze_payload(**overrides):
    """A CRM mirror delivery. ``group_id`` omitted on purpose — callers add it when scoping."""
    return {
        "lms_student_id": 1,
        "status": "active",
        "freeze_start": (date.today() - timedelta(days=10)).isoformat(),
        "planned_resume_date": (date.today() + timedelta(days=20)).isoformat(),
        "reason_code": "vacation",
        "responsible_curator_id": 5,
        "crm_freeze_period_id": 77,
        "revision": 100,
        **overrides,
    }
