"""Таблица parent_reports: один отчёт на ученика в неделю, снапшот фактов не мутирует."""
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


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
def student(db):
    from src.schemas.models import UserInDB
    user = UserInDB(name="Амир", email="amir-pr-model@test.kz",
                    role="student", hashed_password="x")
    db.add(user)
    db.flush()
    return user


def test_report_round_trips(db, student):
    from src.reports.parent.models import ParentReport
    report = ParentReport(
        student_id=student.id, week_start=date(2026, 9, 14), template_key="t1",
        template_auto=True, facts_json={"test": {"verbal": {"correct": 17}}},
        body_generated="текст", body="текст", created_by=student.id,
    )
    db.add(report)
    db.flush()
    stored = db.query(ParentReport).filter(ParentReport.id == report.id).one()
    assert stored.facts_json["test"]["verbal"]["correct"] == 17
    assert stored.template_key == "t1"


def test_one_report_per_student_per_week(db, student):
    from src.reports.parent.models import ParentReport
    for _ in range(2):
        db.add(ParentReport(
            student_id=student.id, week_start=date(2026, 9, 14), template_key="t1",
            template_auto=True, facts_json={}, body_generated="a", body="a",
            created_by=student.id,
        ))
    with pytest.raises(IntegrityError):
        db.flush()


def test_different_weeks_coexist(db, student):
    from src.reports.parent.models import ParentReport
    for week in (date(2026, 9, 14), date(2026, 9, 21)):
        db.add(ParentReport(
            student_id=student.id, week_start=week, template_key="t1",
            template_auto=True, facts_json={}, body_generated="a", body="a",
            created_by=student.id,
        ))
    db.flush()
    assert db.query(ParentReport).filter(ParentReport.student_id == student.id).count() == 2


def test_template_auto_has_a_database_level_default(db, student):
    # Писателя в обход ORM в этом проекте уже заводили (CRM пишет таблицы LMS напрямую).
    # Python-дефолт его не спасёт — значение обязано подставляться самой базой.
    db.execute(
        text(
            "INSERT INTO parent_reports "
            "(student_id, week_start, template_key, facts_json, body_generated, body) "
            "VALUES (:sid, :week, 't1', '{}'::jsonb, 'текст', 'текст')"
        ),
        {"sid": student.id, "week": date(2026, 9, 14)},
    )
    stored = db.execute(
        text("SELECT template_auto FROM parent_reports WHERE student_id = :sid"),
        {"sid": student.id},
    ).scalar_one()
    assert stored is True
