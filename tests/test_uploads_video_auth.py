"""Video bytes require a signed token; everything else under /uploads keeps working.

The unauthorised case returns 404 rather than 401 deliberately. A 401 confirms
that a given path exists, which hands an enumerator exactly the signal the old
unguessable-filename scheme was relying on. There is no session to challenge
here either — the token is the whole credential — so there is nothing useful for
the client to retry with.

Two things about how these tests are written are load-bearing:

*Storage is booby-trapped, not merely absent.* A missing file 404s just as a
refused one does, so a test that only asserts 404 stays green even if the guard
is deleted. ``_explode_on_storage`` makes any storage call fail loudly, and every
guard test runs under both backends, so a regression goes red whichever one
production is configured with.

*The dot segments are percent-encoded.* httpx collapses a literal
``/uploads/./videos/x`` client-side, so a test written that way would never send
the attack. Nothing normalises on the wire: uvicorn hands Starlette the raw path,
so a plain ``/uploads/./videos/x`` from curl arrives with its dots intact and is
the very same request the ``%2e`` spellings below make.
"""
import pytest
from fastapi.testclient import TestClient

from src.services import storage_service
from src.services.media_tokens import mint_media_token

# Both shapes reached the bytes with no token before the request path was
# normalised: ``is_video`` classified them by their first segment (``.`` and
# ``materials``) while the fetch resolved them to the same real video file.
DOT_SEGMENT_BYPASSES = [
    "/uploads/%2e/videos/42/ru/master.m3u8",
    "/uploads/materials/%2e%2e/videos/42/ru/master.m3u8",
]


@pytest.fixture
def client():
    """Build the real app's ``TestClient`` lazily, inside the test.

    ``src.app`` calls ``init_db()`` at import time, which connects to Postgres.
    Importing it at module scope — as this file used to — aborts collection of
    the *entire* suite when no database is reachable. Skip instead, the same
    way ``tests/test_attendance_future_lesson_guard.py`` skips its ``db``
    fixture.
    """
    from sqlalchemy.exc import OperationalError

    try:
        from src.app import app
    except OperationalError:
        pytest.skip("No database available")
    return TestClient(app)


@pytest.fixture(params=["local", "s3"])
def any_backend(request, monkeypatch):
    """Run a guard test against both storage branches.

    ``serve_upload`` must refuse before it reaches storage either way, and the S3
    branch is the one production traffic goes through.
    """
    monkeypatch.setattr(storage_service, "use_s3", lambda: request.param == "s3")
    return request.param


def _explode_on_storage(monkeypatch, why):
    """Turn every storage entry point into a tripwire: ``local_path`` for the local
    backend, ``open_stream`` and ``url_for`` for S3."""

    def _boom(*args, **kwargs):
        raise AssertionError(why)

    for name in ("local_path", "open_stream", "url_for"):
        monkeypatch.setattr(storage_service, name, _boom)


def test_video_without_token_is_not_served(client, any_backend, monkeypatch):
    _explode_on_storage(
        monkeypatch, "serve_upload must 404 on the is_video guard before touching storage"
    )
    r = client.get("/uploads/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


@pytest.mark.parametrize("url", DOT_SEGMENT_BYPASSES)
def test_dot_segments_do_not_smuggle_video_past_the_guard(url, client, any_backend, monkeypatch):
    _explode_on_storage(
        monkeypatch, f"dot segments walked past the is_video guard: {url}"
    )
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 404


def test_video_with_a_valid_token_is_served(client, monkeypatch, tmp_path):
    """The positive case. Without it, a regression that 404s *everything* — the
    guard swallowing legitimate playback — would leave every other test green."""
    playlist = tmp_path / "master.m3u8"
    playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n")

    monkeypatch.setattr(storage_service, "use_s3", lambda: False)
    # Returning the file only for the exact normalised key also pins what the
    # route hands to storage: the bare key, dots already collapsed.
    monkeypatch.setattr(
        storage_service,
        "local_path",
        lambda key: playlist if key == "videos/42/ru/master.m3u8" else None,
    )

    token = mint_media_token("videos/42/ru", user_id=7)
    r = client.get(f"/uploads/v/{token}/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "#EXTM3U\n#EXT-X-VERSION:3\n"


def test_video_with_token_for_another_lesson_is_not_served(client):
    token = mint_media_token("videos/43/ru", user_id=7)
    r = client.get(f"/uploads/v/{token}/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


def test_a_token_cannot_be_walked_out_of_its_lesson(client, any_backend, monkeypatch):
    """The signed route normalises before the prefix check, so climbing out of the
    signed directory is compared as the file it actually resolves to."""
    _explode_on_storage(monkeypatch, "traversal escaped the signed prefix")
    token = mint_media_token("videos/42/ru", user_id=7)
    r = client.get(
        f"/uploads/v/{token}/videos/42/ru/%2e%2e/%2e%2e/43/ru/master.m3u8",
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_signed_route_refuses_a_non_video_key(client, any_backend, monkeypatch):
    """A valid token is not a general read capability over ``/uploads/``. Nothing
    mints a token outside ``videos/`` today; this is what keeps a future
    mis-scoped mint from exposing ``exam_proof/``."""
    _explode_on_storage(monkeypatch, "the signed route served a non-video key")
    token = mint_media_token("exam_proof/9", user_id=7)
    r = client.get(f"/uploads/v/{token}/exam_proof/9/report.pdf", follow_redirects=False)
    assert r.status_code == 404


def test_video_with_garbage_token_is_not_served(client):
    r = client.get("/uploads/v/nonsense/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


def test_non_video_path_is_unaffected(client):
    """A missing non-video file still 404s through the ordinary path, not the guard."""
    r = client.get("/uploads/materials/does-not-exist.pdf", follow_redirects=False)
    assert r.status_code in (307, 404)


def test_non_video_file_is_still_served_without_a_token(client, monkeypatch, tmp_path):
    """Course materials, homework attachments and exam proof keep working exactly as
    before — the guard and the normalisation must not touch them."""
    doc = tmp_path / "syllabus.pdf"
    doc.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(storage_service, "use_s3", lambda: False)
    monkeypatch.setattr(
        storage_service,
        "local_path",
        lambda key: doc if key == "materials/syllabus.pdf" else None,
    )

    r = client.get("/uploads/materials/syllabus.pdf", follow_redirects=False)
    assert r.status_code == 200
    assert r.content == b"%PDF-1.4\n"
