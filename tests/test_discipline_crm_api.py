"""What the CRM reads to put fines on a payslip.

The rule lives here and only here: the CRM asks, it never recomputes. One call answers a whole
half-month for every teacher (the payroll statement and the registry read that), and one answers a
single teacher with the lessons behind the total, for the line an accountant opens.

An open period is served too, marked `closed: false` — accountants want to see the half-month
building up — and a closed one answers from its frozen totals, because payroll was paid on them.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.discipline import service
from src.routes.crm_internal import router as crm_internal_router

KEY = "test-crm-service-key"
NOW = datetime(2026, 10, 1, 0, 0)


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
        session.close()
        trans.rollback()
        connection.close()


def _person(db, role, name):
    from src.schemas.models import UserInDB
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"crm-{datetime.now().timestamp():.6f}-{role}@test.local", name=name,
                    role=role, hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def world(db, monkeypatch):
    from src.schemas.models import Event, EventGroup, Group, GroupStudent
    teacher = _person(db, "teacher", "Кенжебаев Арсен")
    group = Group(name="SAT July 16", is_active=True, is_over=False, program_type="SAT",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=_person(db, "student", "Ученик").id))
    lesson = Event(title="SAT, урок 7", event_type="class",
                   start_datetime=datetime(2026, 9, 17, 13, 0),
                   end_datetime=datetime(2026, 9, 17, 14, 0), created_by=teacher.id,
                   teacher_id=teacher.id, is_active=True)
    db.add(lesson)
    db.flush()
    db.add(EventGroup(event_id=lesson.id, group_id=group.id))
    db.flush()

    monkeypatch.setattr(service, "_timings", lambda db_, events, now: {
        lesson.id: ("ready", datetime(2026, 9, 17, 13, 3), datetime(2026, 9, 17, 14, 0), 8, 9)})
    monkeypatch.setattr(service, "_now", lambda: NOW)
    monkeypatch.setenv("CRM_INTERNAL_SERVICE_KEY", KEY)
    return {"teacher": teacher, "lesson": lesson, "group": group,
            "head": _person(db, "head_teacher", "Head")}


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(crm_internal_router, prefix="/internal/crm")
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _get(client, path):
    return client.get(path, headers={"X-CRM-Service-Key": KEY})


def test_a_period_answers_every_teacher_with_something_owed(client, world):
    body = _get(client, "/internal/crm/discipline/period?period=2026-09-16").json()
    assert body["period"] == {"key": "2026-09-16", "label": "16–30 September 2026",
                              "start": "2026-09-16", "end": "2026-09-30", "closed": False}
    assert body["rate_per_minute"] == 200
    row = next(r for r in body["teachers"] if r["teacher_id"] == world["teacher"].id)
    assert (row["late_minutes"], row["misses"], row["fine"]) == (3, 0, 600)
    assert row["name"] == "Кенжебаев Арсен"
    assert row["unpriced"] == 0


def test_one_teacher_answers_with_the_lessons_behind_the_total(client, world):
    body = _get(client, f"/internal/crm/discipline/teacher/{world['teacher'].id}?period=2026-09-16").json()
    assert body["fine"] == 600
    assert body["closed"] is False
    lesson = body["lessons"][0]
    assert lesson["event_id"] == world["lesson"].id
    assert lesson["group"] == "SAT July 16"
    assert lesson["kind"] == "late"
    assert lesson["minutes"] == 3
    assert lesson["amount"] == 600
    assert lesson["date"] == "2026-09-17"


def test_a_waived_fine_reaches_the_crm_as_zero_with_its_reason(client, world, db):
    from datetime import date
    service.apply_decision(db, actor=world["head"], event_id=world["lesson"].id,
                           teacher_id=world["teacher"].id, day=date(2026, 9, 17), kind="late",
                           amount=0, reason_code="moved", note="перенесли урок")
    body = _get(client, f"/internal/crm/discipline/teacher/{world['teacher'].id}?period=2026-09-16").json()
    assert body["fine"] == 0
    lesson = body["lessons"][0]
    assert lesson["amount"] == 0
    assert lesson["waived"] is True
    assert lesson["reason"] == "Урок перенесён или отменён"
    assert lesson["note"] == "перенесли урок"
    assert lesson["decided_by"] == "Head"


def test_a_closed_period_answers_from_its_frozen_totals(client, world, db):
    from src.discipline.rules import period_containing
    from datetime import date
    service.close_period(db, period_containing(date(2026, 9, 17)), world["head"], now=NOW)
    body = _get(client, "/internal/crm/discipline/period?period=2026-09-16").json()
    assert body["period"]["closed"] is True
    assert body["teachers"][0]["fine"] == 600


def test_the_crm_key_is_required(client, world):
    assert client.get("/internal/crm/discipline/period?period=2026-09-16").status_code == 401
    assert client.get("/internal/crm/discipline/period?period=2026-09-16",
                      headers={"X-CRM-Service-Key": "wrong"}).status_code == 401


def test_a_period_before_the_rule_is_refused_by_name(client, world):
    response = _get(client, "/internal/crm/discipline/period?period=2026-09-01")
    assert response.status_code == 400
    assert "16.09.2026" in response.json()["detail"]


def test_a_teacher_with_nothing_owed_answers_zero_rather_than_404(client, world):
    """A payslip asks for every teacher it pays; silence must read as «owes nothing»."""
    body = _get(client, "/internal/crm/discipline/teacher/999999?period=2026-09-16").json()
    assert body["fine"] == 0
    assert body["lessons"] == []
