"""Who may read the register, and who may price a fine.

Head teachers and admins read every teacher and decide. A teacher reads their own row and decides
nothing — the register is about their money, so they must see it, and must not be able to waive it.
"""
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.discipline import service
from src.discipline.routes.discipline import router as discipline_router
from src.routes.auth import get_current_user_dependency

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
    user = UserInDB(email=f"dr-{datetime.now().timestamp():.6f}-{role}@test.local", name=name,
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
                   teacher_id=teacher.id, is_active=True,
                   meeting_url="https://meet.google.com/abc-defg-hij")
    db.add(lesson)
    db.flush()
    db.add(EventGroup(event_id=lesson.id, group_id=group.id))
    db.flush()

    monkeypatch.setattr(service, "_timings", lambda db_, events, now: {
        lesson.id: ("ready", datetime(2026, 9, 17, 13, 3), datetime(2026, 9, 17, 14, 0), 8, 9)})
    monkeypatch.setattr(service, "_now", lambda: NOW)
    return {"teacher": teacher, "lesson": lesson,
            "head": _person(db, "head_teacher", "Head"), "admin": _person(db, "admin", "Admin"),
            "student": _person(db, "student", "Кто-то"),
            "other_teacher": _person(db, "teacher", "Другой")}


@pytest.fixture
def client(db):
    def _for(user):
        app = FastAPI()
        app.include_router(discipline_router, prefix="/teacher-discipline")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user_dependency] = lambda: user
        return TestClient(app)
    return _for


def test_a_head_teacher_reads_every_teacher(client, world):
    body = client(world["head"]).get("/teacher-discipline/register?period=2026-09-16").json()
    assert [row["teacher_id"] for row in body["teachers"]] == [world["teacher"].id]
    assert body["period"]["label"] == "16–30 September 2026"
    assert body["totals"]["fine"] == 900


def test_a_teacher_reads_only_themselves(client, world):
    body = client(world["teacher"]).get("/teacher-discipline/register?period=2026-09-16").json()
    assert [row["teacher_id"] for row in body["teachers"]] == [world["teacher"].id]

    other = client(world["other_teacher"]).get("/teacher-discipline/register?period=2026-09-16").json()
    assert other["teachers"] == []


def test_a_teacher_cannot_waive_their_own_fine(client, world):
    response = client(world["teacher"]).post("/teacher-discipline/decisions", json={
        "event_id": world["lesson"].id, "teacher_id": world["teacher"].id, "day": "2026-09-17",
        "kind": "late", "amount": 0, "reason_code": "moved"})
    assert response.status_code == 403


def test_a_head_teacher_waives_with_a_reason(client, world):
    response = client(world["head"]).post("/teacher-discipline/decisions", json={
        "event_id": world["lesson"].id, "teacher_id": world["teacher"].id, "day": "2026-09-17",
        "kind": "late", "amount": 0, "reason_code": "moved", "note": "перенесли урок"})
    assert response.status_code == 200
    body = client(world["head"]).get("/teacher-discipline/register?period=2026-09-16").json()
    assert body["totals"]["fine"] == 0


def test_a_student_sees_nothing_of_it(client, world):
    assert client(world["student"]).get(
        "/teacher-discipline/register?period=2026-09-16").status_code == 403


def test_a_period_before_the_rule_is_refused(client, world):
    response = client(world["head"]).get("/teacher-discipline/register?period=2026-09-01")
    assert response.status_code == 400
    assert "16.09.2026" in response.json()["detail"]


def test_the_day_panel_names_the_lesson_and_its_students(client, world):
    body = client(world["head"]).get(
        f"/teacher-discipline/day?teacher_id={world['teacher'].id}&day=2026-09-17").json()
    lesson = body["lessons"][0]
    assert lesson["group"] == "SAT July 16"
    assert (lesson["students"], lesson["students_at_end"]) == (9, 8)
    assert lesson["findings"][0]["kind"] == "late"


def test_a_teacher_cannot_read_another_teachers_day(client, world):
    response = client(world["teacher"]).get(
        f"/teacher-discipline/day?teacher_id={world['other_teacher'].id}&day=2026-09-17")
    assert response.status_code == 403


def test_closing_a_period_freezes_it_and_refuses_later_decisions(client, world):
    head = client(world["head"])
    assert head.post("/teacher-discipline/periods/2026-09-16/close").status_code == 200
    body = head.get("/teacher-discipline/register?period=2026-09-16").json()
    assert body["period"]["closed"] is True

    response = head.post("/teacher-discipline/decisions", json={
        "event_id": world["lesson"].id, "teacher_id": world["teacher"].id, "day": "2026-09-17",
        "kind": "late", "amount": 0, "reason_code": "moved"})
    assert response.status_code == 409


def test_a_teacher_cannot_close_a_period(client, world):
    assert client(world["teacher"]).post(
        "/teacher-discipline/periods/2026-09-16/close").status_code == 403


def test_the_period_list_starts_at_the_rule(client, world):
    body = client(world["head"]).get("/teacher-discipline/periods").json()
    assert body["periods"][-1]["key"] == "2026-09-16"
    assert all(period["start"] >= "2026-09-16" for period in body["periods"])


def test_the_register_imports_on_its_own():
    """Importing the routes first must not deadlock the package graph.

    `src/routes/__init__.py` imports every router inside `register_routes()` for this reason: at
    module level, a router that itself imports `src.routes.auth` is a circular import the moment
    anything reaches the discipline package first — which is what a test, or a script, does.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import src.discipline.routes as r; print(r.discipline_router and 'ok')"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "PYTHONPATH": "."})
    assert result.returncode == 0, result.stderr[-800:]
    assert "ok" in result.stdout


def test_the_export_downloads_the_familiar_sheet(client, world):
    response = client(world["head"]).get("/teacher-discipline/export.xlsx?period=2026-09-16")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert "discipline-2026-09-16.xlsx" in response.headers["content-disposition"]

    from io import BytesIO

    from openpyxl import load_workbook
    sheet = load_workbook(BytesIO(response.content))["SAT"]
    assert sheet["A1"].value == "ФИО"
    assert sheet["A3"].value == "Кенжебаев Арсен"


def test_a_student_cannot_download_the_register(client, world):
    assert client(world["student"]).get(
        "/teacher-discipline/export.xlsx?period=2026-09-16").status_code == 403
