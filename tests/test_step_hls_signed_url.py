"""The stored HLS path is turned into a playable, token-bearing URL per viewer.

The token is minted against the playlist's *directory*, not the playlist file,
so the variant playlists and .ts segments beside it are covered by the same
token the player already holds.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.services.media_tokens import signed_hls_url, verify_media_token
from tests.checkpoint_fixtures import enroll, make_group, make_sat_course, make_user


def test_signed_url_points_at_the_guarded_route():
    url = signed_hls_url("/uploads/videos/42/ru/master.m3u8", user_id=7)
    assert url.startswith("/uploads/v/")
    assert url.endswith("/videos/42/ru/master.m3u8")


def test_signed_url_token_covers_the_sibling_segments():
    url = signed_hls_url("/uploads/videos/42/ru/master.m3u8", user_id=7)
    token = url.split("/uploads/v/", 1)[1].split("/", 1)[0]
    assert verify_media_token(token, "videos/42/ru/v0_017.ts") is not None


def test_none_in_none_out():
    assert signed_hls_url(None, user_id=7) is None


# ------------------------------------------------------- the endpoint call site


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
        session.close(); trans.rollback(); connection.close()


def _client(db, user):
    from src.courses.routes.courses import router as courses_router
    app = FastAPI()
    app.include_router(courses_router, prefix="/courses")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user_dependency] = lambda: user
    return TestClient(app)


def test_get_step_returns_a_signed_hls_url(db):
    """GET /courses/steps/{id} mints a per-viewer token rather than handing back
    the bare storage path — that's what makes the route in Task 2 reachable at all."""
    admin = make_user(db, role="admin")
    course, verbal, _ = make_sat_course(db, n_verbal=1, n_math=1)
    group = make_group(db, enabled=False)
    student = make_user(db)
    enroll(db, student, group, course, admin)
    lesson = verbal[0]
    step = lesson.steps[0]
    step.hls_url = "/uploads/videos/999/ru/master.m3u8"
    db.flush()

    r = _client(db, student).get(f"/courses/steps/{step.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["hls_url"].startswith("/uploads/v/")
    assert body["hls_url"].endswith("/videos/999/ru/master.m3u8")
