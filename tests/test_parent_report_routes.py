"""Доступ к родительским отчётам и upsert по (ученик, неделя)."""
from datetime import date

import pytest
from fastapi import HTTPException


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()


@pytest.fixture
def curator_and_group(db):
    from src.schemas.models import Group, GroupStudent, UserInDB
    curator = UserInDB(name="Айгерим", email="curator-pr@test.kz",
                       role="curator", hashed_password="x")
    stranger = UserInDB(name="Другой", email="curator2-pr@test.kz",
                        role="curator", hashed_password="x")
    student = UserInDB(name="Амир", email="amir-pr-routes@test.kz",
                       role="student", hashed_password="x")
    db.add_all([curator, stranger, student])
    db.flush()
    group = Group(name="SAT-3", program_type="sat", curator_id=curator.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return curator, stranger, student, group


def test_curator_of_the_group_passes_group_check(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    curator, _, _, group = curator_and_group
    assert _require_group_access(group.id, curator, db).id == group.id


def test_foreign_curator_is_rejected(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    _, stranger, _, group = curator_and_group
    with pytest.raises(HTTPException) as exc:
        _require_group_access(group.id, stranger, db)
    assert exc.value.status_code == 403


def test_admin_passes_any_group(db, curator_and_group):
    from src.reports.parent.routes import _require_group_access
    from src.schemas.models import UserInDB
    _, _, _, group = curator_and_group
    admin = UserInDB(name="Админ", email="admin-pr@test.kz",
                     role="admin", hashed_password="x")
    db.add(admin)
    db.flush()
    assert _require_group_access(group.id, admin, db).id == group.id


def test_save_upserts_instead_of_duplicating(db, curator_and_group):
    from src.reports.parent.models import ParentReport
    from src.reports.parent.routes import _upsert
    curator, _, student, group = curator_and_group

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={"a": 1},
            body_generated="первый", curator_note=None, user_id=curator.id)
    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t5", template_auto=False, facts={"a": 2},
            body_generated="второй", curator_note="заметка", user_id=curator.id)

    rows = db.query(ParentReport).filter(ParentReport.student_id == student.id).all()
    assert len(rows) == 1
    assert rows[0].template_key == "t5"
    assert rows[0].body_generated == "второй"
    assert rows[0].facts_json == {"a": 2}


def test_curator_edit_survives_but_regeneration_replaces_it(db, curator_and_group):
    from src.reports.parent.models import ParentReport
    from src.reports.parent.routes import _upsert
    curator, _, student, group = curator_and_group

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={}, body_generated="исходный",
            curator_note=None, user_id=curator.id)
    row = db.query(ParentReport).filter(ParentReport.student_id == student.id).one()
    row.body = "поправленный куратором"
    db.flush()

    _upsert(db, student_id=student.id, week_start=date(2026, 9, 14), group_id=group.id,
            template_key="t1", template_auto=True, facts={}, body_generated="новый",
            curator_note=None, user_id=curator.id)
    row = db.query(ParentReport).filter(ParentReport.student_id == student.id).one()
    assert row.body == "новый"
    assert row.body_generated == "новый"


@pytest.fixture
def client(db, curator_and_group, monkeypatch):
    """Настоящие эндпоинты, но без сети: платформы и LLM подменены."""
    from fastapi.testclient import TestClient
    from src.app import app
    from src.config import get_db
    from src.routes.auth import get_current_user_dependency

    curator, _, _, _ = curator_and_group

    async def _no_platforms(db_, student):
        return {"sat": [], "ielts": [], "nuet": [], "errors": []}

    async def _fake_prose(facts, template_key, client=None):
        return {"progress": "Динамика ровная.",
                "recommendation": "Читать одну статью на английском в день."}

    monkeypatch.setattr("src.reports.parent.facts.fetch_weekly_tests", _no_platforms)
    monkeypatch.setattr("src.reports.parent.routes.generate_prose", _fake_prose)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_dependency] = lambda: curator
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_generate_saves_a_report_and_returns_it(client, curator_and_group):
    _, _, student, _ = curator_and_group
    response = client.post(f"/reports/parent/students/{student.id}",
                           json={"week": "2026-09-16"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["week_start"] == "2026-09-14"
    assert payload["prose_degraded"] is False
    assert "Читать одну статью" in payload["report"]["body"]


def test_get_returns_the_saved_report_without_regenerating(client, curator_and_group):
    _, _, student, _ = curator_and_group
    client.post(f"/reports/parent/students/{student.id}", json={"week": "2026-09-16"})
    response = client.get(f"/reports/parent/students/{student.id}",
                          params={"week": "2026-09-16"})
    assert response.status_code == 200
    assert response.json()["report"]["body"]


def test_any_day_of_the_week_addresses_the_same_report(client, curator_and_group, db):
    # Куратор мог открыть страницу в среду, а перегенерировать в пятницу.
    from src.reports.parent.models import ParentReport
    _, _, student, _ = curator_and_group
    client.post(f"/reports/parent/students/{student.id}", json={"week": "2026-09-16"})
    client.post(f"/reports/parent/students/{student.id}", json={"week": "2026-09-18"})
    rows = db.query(ParentReport).filter(ParentReport.student_id == student.id).all()
    assert len(rows) == 1


def test_curator_edit_round_trips_through_put(client, curator_and_group):
    _, _, student, _ = curator_and_group
    client.post(f"/reports/parent/students/{student.id}", json={"week": "2026-09-16"})
    saved = client.put(f"/reports/parent/students/{student.id}",
                       json={"week": "2026-09-16", "body": "Мой текст"})
    assert saved.status_code == 200
    again = client.get(f"/reports/parent/students/{student.id}",
                       params={"week": "2026-09-16"})
    assert again.json()["report"]["body"] == "Мой текст"


def test_put_before_anything_was_generated_is_404(client, curator_and_group):
    _, _, student, _ = curator_and_group
    response = client.put(f"/reports/parent/students/{student.id}",
                          json={"week": "2026-09-16", "body": "Мой текст"})
    assert response.status_code == 404


def test_group_overview_lists_students_and_their_report_state(client, curator_and_group):
    _, _, student, group = curator_and_group
    before = client.get(f"/reports/parent/groups/{group.id}", params={"week": "2026-09-16"})
    assert before.status_code == 200
    assert [s["id"] for s in before.json()["students"]] == [student.id]
    assert before.json()["students"][0]["report"] is None

    client.post(f"/reports/parent/students/{student.id}", json={"week": "2026-09-16"})
    after = client.get(f"/reports/parent/groups/{group.id}", params={"week": "2026-09-16"})
    assert after.json()["students"][0]["report"] is not None


def test_report_is_still_saved_when_prose_generation_fails(client, curator_and_group, monkeypatch):
    # Каркас с числами важнее прозы: куратор допишет руками, но пустого экрана не увидит.
    async def _no_prose(facts, template_key, client=None):
        return {}

    monkeypatch.setattr("src.reports.parent.routes.generate_prose", _no_prose)
    _, _, student, _ = curator_and_group
    response = client.post(f"/reports/parent/students/{student.id}",
                           json={"week": "2026-09-16"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["prose_degraded"] is True
    assert payload["report"]["body"]
