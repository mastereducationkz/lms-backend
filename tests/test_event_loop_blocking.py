"""Blocking work must stay off the event loop.

uvicorn runs FastAPI and Socket.IO on one event loop per worker. FastAPI awaits an ``async def``
dependency on that loop, and a Socket.IO handler always runs there. On 2026-09-17 (18:17–18:32
Almaty) the auth dependency behind 438 of 506 routes was ``async def``: its token check (an HTTP
call to Zitadel on a cache miss) and its user query (waiting for a pgbouncer slot) froze whole
workers. Requests already inside a transaction sat idle, the pool ran dry, Socket.IO clients
reconnected by the hundreds a minute — each connect doing more blocking work on the loop — and the
API answered 503 until the storm passed.
"""
import asyncio
import inspect
import threading

import pytest
from fastapi.dependencies.utils import is_coroutine_callable

from src.schemas.models import Group, GroupConversation, UserInDB


def test_auth_dependencies_are_plain_functions():
    from src.routes import auth

    for dependency in (auth.get_current_user_dependency, auth.require_admin, auth.require_teacher_or_admin):
        assert not inspect.iscoroutinefunction(dependency), f"{dependency.__name__} would run on the event loop"


def test_a_slow_token_check_does_not_stall_other_requests(monkeypatch):
    """The incident in miniature: one request's token check is slow (Zitadel, a busy pool);
    a request that needs no auth at all must still answer at once."""
    import time

    import httpx
    from sqlalchemy.exc import OperationalError

    try:
        from src.app import app
    except OperationalError:
        pytest.skip("No database available")
    from src.routes import auth

    def slow_token_check(token):
        time.sleep(1.0)
        return None

    monkeypatch.setattr(auth, "verify_bearer_token", slow_token_check)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://lms.test") as client:
            # Both clocks start when the slow request is sent: a blocked loop would also delay the
            # 0.1 s sleep below, so timing the fast request from its own start would hide the stall.
            started = time.perf_counter()
            slow = asyncio.create_task(client.get("/gamification/status", headers={"Authorization": "Bearer t"}))
            await asyncio.sleep(0.1)
            fast_response = await client.get("/")
            fast_seconds = time.perf_counter() - started
            slow_response = await slow
            return fast_response, fast_seconds, slow_response, time.perf_counter() - started

    fast_response, fast_seconds, slow_response, slow_seconds = asyncio.run(main())
    assert slow_response.status_code == 401 and slow_seconds >= 1.0
    assert fast_response.status_code == 200
    assert fast_seconds < 0.6, f"an unauthenticated request waited {fast_seconds:.2f}s behind a slow token check"


def test_no_route_depends_on_an_async_dependency_of_ours():
    from sqlalchemy.exc import OperationalError
    from fastapi.routing import APIRoute

    try:
        from src.app import app
    except OperationalError:
        pytest.skip("No database available")

    def async_dependencies(dependant, found):
        for sub in dependant.dependencies:
            call = sub.call
            # FastAPI's own security schemes (OAuth2PasswordBearer) are async but only read headers.
            if call is not None and is_coroutine_callable(call) and getattr(call, "__module__", "").startswith("src."):
                found.add(f"{call.__module__}.{call.__qualname__}")
            async_dependencies(sub, found)
        return found

    offenders = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for name in async_dependencies(route.dependant, set()):
                offenders.setdefault(name, []).append(route.path)
    assert offenders == {}, "async dependencies run on the event loop: " + ", ".join(
        f"{name} ({len(paths)} routes)" for name, paths in offenders.items())


class _FakeSio:
    def __init__(self):
        self.rooms, self.sessions, self.disconnected = [], {}, []

    async def enter_room(self, sid, room):
        self.rooms.append(room)

    async def save_session(self, sid, data):
        self.sessions[sid] = data

    async def get_session(self, sid):
        return self.sessions.get(sid)

    async def disconnect(self, sid):
        self.disconnected.append(sid)


def _run_on_a_loop(coroutine_factory):
    """Run a coroutine and return (result, the loop thread's id)."""
    async def main():
        return await coroutine_factory(), threading.get_ident()
    return asyncio.run(main())


@pytest.fixture
def fake_sio(monkeypatch):
    from src.messages.routes import socket_messages
    fake = _FakeSio()
    for name in ("enter_room", "save_session", "get_session", "disconnect"):
        monkeypatch.setattr(socket_messages.sio, name, getattr(fake, name))
    return fake


def test_connect_checks_the_token_and_reads_rooms_off_the_event_loop(monkeypatch, fake_sio):
    from src.messages.routes import socket_messages

    threads = {}

    def fake_token_check(environ, auth):
        threads["token"] = threading.get_ident()
        return 7

    def fake_rooms(user_id):
        threads["rooms"] = threading.get_ident()
        return [11, 12]

    monkeypatch.setattr(socket_messages, "_get_user_id_from_environ", fake_token_check)
    monkeypatch.setattr(socket_messages, "_group_conversation_ids", fake_rooms)

    _, loop_thread = _run_on_a_loop(lambda: socket_messages.connect("sid-1", {}, {"token": "t"}))

    assert threads["token"] != loop_thread and threads["rooms"] != loop_thread
    assert fake_sio.sessions == {"sid-1": {"user_id": 7}}
    assert fake_sio.rooms == ["user:7", "group:11", "group:12"]
    assert fake_sio.disconnected == []


def test_connect_rejects_a_bad_token_without_touching_rooms(monkeypatch, fake_sio):
    from src.messages.routes import socket_messages

    monkeypatch.setattr(socket_messages, "_get_user_id_from_environ", lambda environ, auth: None)
    monkeypatch.setattr(socket_messages, "_group_conversation_ids",
                        lambda user_id: pytest.fail("rooms read for a rejected connection"))

    _run_on_a_loop(lambda: socket_messages.connect("sid-2", {}, {"token": "expired"}))

    assert fake_sio.disconnected == ["sid-2"]
    assert fake_sio.rooms == [] and fake_sio.sessions == {}


def test_connect_still_joins_the_user_room_when_group_rooms_fail(monkeypatch, fake_sio):
    from src.messages.routes import socket_messages

    def broken_rooms(user_id):
        raise RuntimeError("server closed the connection unexpectedly")

    monkeypatch.setattr(socket_messages, "_get_user_id_from_environ", lambda environ, auth: 7)
    monkeypatch.setattr(socket_messages, "_group_conversation_ids", broken_rooms)

    _run_on_a_loop(lambda: socket_messages.connect("sid-3", {}, None))

    assert fake_sio.rooms == ["user:7"]
    assert fake_sio.disconnected == []


def test_unread_count_is_counted_off_the_event_loop(monkeypatch, fake_sio):
    from src.messages.routes import socket_messages

    threads = {}

    def fake_count(session):
        threads["count"] = threading.get_ident()
        return {"unread_count": 3}

    monkeypatch.setattr(socket_messages, "_unread_count", fake_count)
    fake_sio.sessions["sid-4"] = {"user_id": 7}

    result, loop_thread = _run_on_a_loop(lambda: socket_messages.handle_unread_count("sid-4"))

    assert result == {"unread_count": 3}
    assert threads["count"] != loop_thread


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
        session.close()
        trans.rollback()
        connection.close()


class _KeepOpen:
    """The test's session, handed out as if fresh; closing it must not end the test transaction."""
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def test_group_rooms_are_the_conversations_the_chat_list_shows(monkeypatch, db):
    """The light id query joins exactly the rooms the old per-connect `list_conversations` did."""
    from src.messages.group_membership import ensure_group_conversations
    from src.messages.group_service import list_conversations
    from src.messages.routes import socket_messages
    from src.schemas.models import GroupStudent
    from src.utils.auth_utils import hash_password

    def user(email, role):
        u = UserInDB(email=email, name=email.split("@")[0], role=role,
                     hashed_password=hash_password("x"), is_active=True)
        db.add(u); db.flush()
        return u

    teacher, student, stranger = (user("elb-t@test.local", "teacher"), user("elb-s@test.local", "student"),
                                  user("elb-x@test.local", "student"))
    group = Group(name="ELB", is_active=True, teacher_id=teacher.id)
    db.add(group); db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id)); db.flush()
    ensure_group_conversations(db, group); db.flush()
    monkeypatch.setattr(socket_messages, "SessionLocal", lambda: _KeepOpen(db))

    class_chat = db.query(GroupConversation).filter_by(group_id=group.id, kind="class").one()
    assert class_chat.id in socket_messages._group_conversation_ids(student.id)
    for person in (teacher, student, stranger):
        assert sorted(socket_messages._group_conversation_ids(person.id)) == sorted(
            c["id"] for c in list_conversations(db, person.id)), person.email
    assert socket_messages._group_conversation_ids(stranger.id) == []


def test_recommendations_release_the_database_before_calling_the_external_api(monkeypatch):
    """The user lookup opens a transaction; waiting on the external API (up to 2 × 30 s) inside it
    would hold one of pgbouncer's server connections the whole time."""
    import httpx
    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from src.config import SessionLocal
    from src.gamification.routes import daily_questions

    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No database available")
    assert session.in_transaction()
    seen = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            seen["in_transaction"] = session.in_transaction()
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(daily_questions.httpx, "AsyncClient", FakeClient)
    student = UserInDB(id=1, email="elb-rec@test.local", role="student", name="elb")
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(daily_questions.get_daily_question_recommendations(current_user=student, db=session))
    finally:
        session.close()
    assert error.value.status_code == 502
    assert seen == {"in_transaction": False}
