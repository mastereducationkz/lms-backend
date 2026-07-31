"""Report-message flow (App Store guideline 1.2 — UGC moderation).

Participants of a direct-message conversation can flag a message for admin
review; admins can list reports. Non-participants are rejected, and repeat
reports by the same user don't create duplicates.
"""
import pytest

from src.schemas.models import UserInDB
from src.messages.models import Message, MessageReport
from src.messages.schemas import ReportMessageSchema
from src.messages.routes.messages import report_message, list_message_reports
from src.utils.auth_utils import hash_password
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
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()
    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close(); trans.rollback(); connection.close()


def _u(db, email, role="student"):
    u = UserInDB(email=email, name=email.split("@")[0], role=role,
                 hashed_password=hash_password("x"), is_active=True)
    db.add(u); db.flush(); return u


def _msg(db, sender, recipient, content="hello"):
    m = Message(from_user_id=sender.id, to_user_id=recipient.id, content=content)
    db.add(m); db.flush(); return m


def test_participant_can_report_message(db):
    student = _u(db, "mr-student@test.local", "student")
    teacher = _u(db, "mr-teacher@test.local", "teacher")
    msg = _msg(db, teacher, student)

    result = report_message(
        message_id=msg.id,
        payload=ReportMessageSchema(reason="inappropriate"),
        current_user=student,
        db=db,
    )

    report = db.query(MessageReport).filter(MessageReport.message_id == msg.id).one()
    assert report.reporter_id == student.id
    assert report.reason == "inappropriate"
    assert report.status == "open"
    assert result["status"] == "reported"


def test_non_participant_cannot_report(db):
    student = _u(db, "mr2-student@test.local", "student")
    teacher = _u(db, "mr2-teacher@test.local", "teacher")
    outsider = _u(db, "mr2-outsider@test.local", "student")
    msg = _msg(db, teacher, student)

    with pytest.raises(HTTPException) as exc:
        report_message(
            message_id=msg.id,
            payload=ReportMessageSchema(),
            current_user=outsider,
            db=db,
        )
    assert exc.value.status_code == 403
    assert db.query(MessageReport).count() == 0


def test_reporting_twice_is_idempotent(db):
    student = _u(db, "mr3-student@test.local", "student")
    teacher = _u(db, "mr3-teacher@test.local", "teacher")
    msg = _msg(db, teacher, student)

    report_message(message_id=msg.id, payload=ReportMessageSchema(),
                   current_user=student, db=db)
    report_message(message_id=msg.id, payload=ReportMessageSchema(),
                   current_user=student, db=db)

    assert db.query(MessageReport).filter(MessageReport.message_id == msg.id).count() == 1


def test_report_missing_message_404(db):
    student = _u(db, "mr4-student@test.local", "student")
    with pytest.raises(HTTPException) as exc:
        report_message(message_id=99999999, payload=ReportMessageSchema(),
                       current_user=student, db=db)
    assert exc.value.status_code == 404


def test_admin_lists_reports_and_non_admin_forbidden(db):
    student = _u(db, "mr5-student@test.local", "student")
    teacher = _u(db, "mr5-teacher@test.local", "teacher")
    admin = _u(db, "mr5-admin@test.local", "admin")
    msg = _msg(db, teacher, student, content="bad message")
    report_message(message_id=msg.id, payload=ReportMessageSchema(reason="spam"),
                   current_user=student, db=db)

    reports = list_message_reports(current_user=admin, db=db)
    ours = [r for r in reports if r["message_id"] == msg.id]
    assert len(ours) == 1
    assert ours[0]["reason"] == "spam"
    assert ours[0]["message_content"] == "bad message"
    assert ours[0]["reporter_id"] == student.id

    with pytest.raises(HTTPException) as exc:
        list_message_reports(current_user=teacher, db=db)
    assert exc.value.status_code == 403
