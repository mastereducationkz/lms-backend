"""The freeze mirror and the health-facts endpoint.

Two contracts the CRM depends on:

* the mirror converges under at-least-once, out-of-order delivery, and never touches access;
* the facts endpoint counts the way the CRM's rules assume — unmarked is missing data,
  cancelled and pre-membership lessons are not denominators, and frozen days drop out.

The counting rules get the most attention because getting them wrong produces confident
nonsense: a curator told a student is at 40% attendance will act on it.
"""
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.curator.freeze_mirror import (
    StudentFreezeState,
    freeze_states,
    frozen_badge,
    is_within_freeze,
    upsert_freeze_state,
)
from src.curator.health_facts import health_facts
from src.routes.crm_curator_internal import router as curator_router
from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, UserInDB
from tests.onboarding_fixtures import db  # noqa: F401

SERVICE_KEY = "test-crm-service-key"


@pytest.fixture
def app(db, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CRM_INTERNAL_SERVICE_KEY", SERVICE_KEY)
    application = FastAPI()
    application.include_router(curator_router, prefix="/internal/crm/curator")
    application.dependency_overrides[get_db] = lambda: db
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


def _hdr():
    return {"X-CRM-Service-Key": SERVICE_KEY, "X-Crm-Staff-Role": "admin"}


@pytest.fixture
def world(db):  # noqa: F811
    """One curator, one group, one student, and a handful of lessons to count."""
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
    group = Group(
        name="SAT-1", curator_id=curator.id, is_active=True, is_over=False,
        program_type="SAT", group_type="group",
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
    db.commit()
    return {"db": db, "curator": curator, "student": student, "group": group}


def _lesson(world, *, days_ago: float, status=None, active=True):
    db = world["db"]
    start = datetime.utcnow() - timedelta(days=days_ago)
    event = Event(
        title="Урок", event_type="class", start_datetime=start,
        end_datetime=start + timedelta(hours=1), is_active=active,
        teacher_id=None, created_by=world["curator"].id,
    )
    db.add(event)
    db.flush()
    db.add(EventGroup(event_id=event.id, group_id=world["group"].id))
    if status is not None:
        db.add(Attendance(event_id=event.id, user_id=world["student"].id, status=status))
    db.commit()
    return event


def _facts(world, **kwargs):
    payload = health_facts(world["db"], [world["student"].id], **kwargs)
    return payload["students"][str(world["student"].id)]


def _group_facts(world, **kwargs):
    return _facts(world, **kwargs)["groups"][str(world["group"].id)]


# --- counting rules -------------------------------------------------------------------------


def test_an_unmarked_lesson_is_not_an_absence(world):
    """It is missing data. Counting it as an absence invents a problem."""
    _lesson(world, days_ago=3, status="present")
    _lesson(world, days_ago=2)  # nobody marked it

    facts = _group_facts(world)
    assert facts["marked_lessons"] == 1
    assert facts["attended_lessons"] == 1
    assert facts["attendance_rate"] == 100.0
    assert facts["unmarked_past_lessons"] == 1


def test_a_cancelled_lesson_is_in_no_denominator(world):
    _lesson(world, days_ago=3, status="present")
    _lesson(world, days_ago=2, status="absent", active=False)

    facts = _group_facts(world)
    assert facts["marked_lessons"] == 1
    assert facts["attendance_rate"] == 100.0


def test_lessons_before_the_student_joined_do_not_count(world):
    db = world["db"]
    db.query(GroupStudent).filter(
        GroupStudent.student_id == world["student"].id
    ).update({"created_at": datetime.utcnow() - timedelta(days=5)})
    db.commit()

    _lesson(world, days_ago=20, status="absent")  # before they joined
    _lesson(world, days_ago=2, status="present")

    facts = _group_facts(world)
    assert facts["marked_lessons"] == 1
    assert facts["attendance_rate"] == 100.0


def test_late_counts_as_attended(world):
    _lesson(world, days_ago=3, status="late")
    facts = _group_facts(world)
    assert facts["attended_lessons"] == 1


def test_a_removed_student_leaves_the_lesson_out_of_both_counts(world):
    _lesson(world, days_ago=3, status="removed")
    facts = _group_facts(world)
    assert facts["marked_lessons"] == 0
    assert facts["unmarked_past_lessons"] == 0


def test_consecutive_absences_count_back_from_the_most_recent_marked_lesson(world):
    _lesson(world, days_ago=6, status="absent")
    _lesson(world, days_ago=5, status="present")
    _lesson(world, days_ago=3, status="absent")
    _lesson(world, days_ago=2, status="absent")

    assert _group_facts(world)["consecutive_absences"] == 2


def test_an_unmarked_gap_cannot_fake_an_absence_streak(world):
    _lesson(world, days_ago=5, status="absent")
    _lesson(world, days_ago=3)  # unmarked — skipped entirely
    _lesson(world, days_ago=2, status="absent")

    assert _group_facts(world)["consecutive_absences"] == 2


def test_a_streak_ends_at_the_most_recent_attendance(world):
    _lesson(world, days_ago=5, status="absent")
    _lesson(world, days_ago=4, status="absent")
    _lesson(world, days_ago=1, status="present")

    assert _group_facts(world)["consecutive_absences"] == 0


def test_frozen_days_leave_the_denominator(world):
    """The freeze must not read as a collapse in attendance."""
    _lesson(world, days_ago=10, status="present")
    _lesson(world, days_ago=4, status="absent")
    _lesson(world, days_ago=3, status="absent")

    frozen = {
        world["student"].id: [
            ((date.today() - timedelta(days=6)).isoformat(), None),
        ]
    }
    facts = _group_facts(world, frozen_windows=frozen)
    assert facts["marked_lessons"] == 1
    assert facts["attendance_rate"] == 100.0
    assert facts["consecutive_absences"] == 0


def test_weeks_before_the_freeze_are_untouched(world):
    _lesson(world, days_ago=20, status="absent")
    _lesson(world, days_ago=19, status="absent")

    frozen = {
        world["student"].id: [((date.today() - timedelta(days=5)).isoformat(), None)]
    }
    facts = _group_facts(world, frozen_windows=frozen)
    assert facts["marked_lessons"] == 2
    assert facts["attendance_rate"] == 0.0


def test_future_lessons_are_counted_separately_and_never_as_absences(world):
    _lesson(world, days_ago=-5)  # five days from now
    facts = _group_facts(world)
    assert facts["future_lessons"] == 1
    assert facts["marked_lessons"] == 0


def test_the_endpoint_returns_the_same_facts_over_http(client, world):
    _lesson(world, days_ago=2, status="absent")
    response = client.post(
        "/internal/crm/curator/students/health-facts",
        json={"lms_student_ids": [world["student"].id]},
        headers=_hdr(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()["students"][str(world["student"].id)]
    assert payload["groups"][str(world["group"].id)]["consecutive_absences"] == 1


def test_the_endpoint_needs_the_service_key(client, world):
    assert client.post(
        "/internal/crm/curator/students/health-facts",
        json={"lms_student_ids": [world["student"].id]},
    ).status_code in (401, 403, 422)


# --- freeze mirror ----------------------------------------------------------------------------


def _payload(**overrides):
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


def test_the_mirror_applies_a_freeze(db):  # noqa: F811
    result = upsert_freeze_state(db, _payload())
    db.commit()
    assert result["applied"] is True
    row = db.query(StudentFreezeState).one()
    assert row.is_frozen is True
    assert row.responsible_curator_id == 5


def test_replaying_the_same_message_converges(db):  # noqa: F811
    upsert_freeze_state(db, _payload())
    upsert_freeze_state(db, _payload())
    db.commit()
    assert db.query(StudentFreezeState).count() == 1


def test_an_out_of_order_older_message_is_ignored(db):  # noqa: F811
    """At-least-once delivery promises nothing about order; a late old message must not
    resurrect a freeze somebody has ended."""
    upsert_freeze_state(db, _payload(revision=200, status="resumed",
                                     actual_resume_date=date.today().isoformat()))
    db.commit()
    result = upsert_freeze_state(db, _payload(revision=100, status="active"))
    db.commit()

    assert result["applied"] is False
    row = db.query(StudentFreezeState).one()
    assert row.status == "resumed"
    assert row.is_frozen is False


def test_a_newer_message_wins(db):  # noqa: F811
    upsert_freeze_state(db, _payload(revision=100))
    db.commit()
    upsert_freeze_state(db, _payload(revision=200, status="resumed",
                                     actual_resume_date=date.today().isoformat()))
    db.commit()
    assert db.query(StudentFreezeState).one().is_frozen is False


def test_the_endpoint_applies_a_batch(client, db):  # noqa: F811
    response = client.post(
        "/internal/crm/curator/students/freeze-state",
        json={"items": [_payload(lms_student_id=1), _payload(lms_student_id=2)]},
        headers=_hdr(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == 2
    assert len(freeze_states(db, [1, 2])) == 2


def test_a_student_sees_the_date_and_never_the_reason(db):  # noqa: F811
    """A student reading «финансовый вопрос» about themselves is a support incident."""
    upsert_freeze_state(db, _payload())
    db.commit()
    row = db.query(StudentFreezeState).one()

    staff = frozen_badge(row)
    student = frozen_badge(row, for_student=True)
    assert "reason_code" in staff and staff["reason_code"] == "vacation"
    assert "reason_code" not in student
    assert student["planned_resume_date"] == row.planned_resume_date.isoformat()
    assert student["label"].startswith("Заморожен до ")


def test_a_resumed_freeze_shows_no_badge(db):  # noqa: F811
    upsert_freeze_state(db, _payload(status="resumed", actual_resume_date=date.today().isoformat()))
    db.commit()
    assert frozen_badge(db.query(StudentFreezeState).one()) is None


def test_counting_resumes_from_the_confirmed_return_not_the_planned_one(db):  # noqa: F811
    upsert_freeze_state(
        db,
        _payload(
            revision=300,
            status="resumed",
            freeze_start=(date.today() - timedelta(days=30)).isoformat(),
            planned_resume_date=(date.today() - timedelta(days=20)).isoformat(),
            actual_resume_date=(date.today() - timedelta(days=5)).isoformat(),
        ),
    )
    db.commit()
    row = db.query(StudentFreezeState).one()

    assert is_within_freeze(row, date.today() - timedelta(days=25))
    # Between the planned and the actual return they were still away.
    assert is_within_freeze(row, date.today() - timedelta(days=10))
    assert not is_within_freeze(row, date.today() - timedelta(days=2))
    assert not is_within_freeze(row, date.today() - timedelta(days=40))
