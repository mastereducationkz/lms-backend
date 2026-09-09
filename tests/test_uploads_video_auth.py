"""Video bytes require a signed token; everything else under /uploads keeps working.

The unauthorised case returns 404 rather than 401 deliberately. A 401 confirms
that a given path exists, which hands an enumerator exactly the signal the old
unguessable-filename scheme was relying on. There is no session to challenge
here either — the token is the whole credential — so there is nothing useful for
the client to retry with.
"""
from fastapi.testclient import TestClient

from src.app import app
from src.services import storage_service
from src.services.media_tokens import mint_media_token

client = TestClient(app)


def test_video_without_token_is_not_served(monkeypatch):
    """A missing local file would also 404, guard or no guard, so that alone can't
    catch a regression that deletes the ``is_video`` check in ``serve_upload``. Make
    the storage lookup explode if it is ever reached, so this only stays green while
    the guard short-circuits before touching storage."""

    def _should_not_be_reached(path):
        raise AssertionError("serve_upload must 404 on the is_video guard before touching storage")

    monkeypatch.setattr(storage_service, "local_path", _should_not_be_reached)
    r = client.get("/uploads/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


def test_video_with_token_for_another_lesson_is_not_served():
    token = mint_media_token("videos/43/ru", user_id=7)
    r = client.get(f"/uploads/v/{token}/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


def test_video_with_garbage_token_is_not_served():
    r = client.get("/uploads/v/nonsense/videos/42/ru/master.m3u8", follow_redirects=False)
    assert r.status_code == 404


def test_non_video_path_is_unaffected():
    """A missing non-video file still 404s through the ordinary path, not the guard."""
    r = client.get("/uploads/materials/does-not-exist.pdf", follow_redirects=False)
    assert r.status_code in (307, 404)
