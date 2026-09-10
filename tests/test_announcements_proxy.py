"""Tests for the /announcements proxy to the Support platform.

No DB needed: these routes hold no LMS state. What they DO hold is the only
human authorization check in the whole feature -- Support authenticates the
call by shared key, not by user token -- so the role gate and the acting-user
header are what these tests protect.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.announcements.routes import announcements as routes
from src.services import support_client
from src.utils.permissions import ROLE_HIERARCHY


def _user(role: str, email: str = "head@mastereducation.kz", name: str = "Head"):
    return SimpleNamespace(role=role, email=email, name=name)


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "head_curator", "head_teacher"])
def test_heads_and_admins_may_broadcast(role):
    checker = routes._announcer()
    assert checker(current_user=_user(role)).role == role


@pytest.mark.parametrize("role", ["student", "parent", "curator", "teacher"])
def test_everyone_else_is_refused(role):
    """A broadcast reaches every student group at once, so it stops at the
    heads. Note `teacher` is refused while `head_teacher` is not -- a
    distinction only this platform can make, because Support maps head_teacher
    to teacher on login."""
    checker = routes._announcer()
    with pytest.raises(HTTPException) as exc:
        checker(current_user=_user(role))
    assert exc.value.status_code == 403


def test_head_teacher_is_a_real_role_here():
    """Guards the premise of the gate above: if head_teacher ever stopped being
    a distinct LMS role, the split enforced in this module would be silently
    meaningless."""
    assert ROLE_HIERARCHY["head_teacher"] == ROLE_HIERARCHY["teacher"]
    assert "head_teacher" in ROLE_HIERARCHY
    assert routes.ANNOUNCER_ROLES == ["admin", "head_curator", "head_teacher"]


# ---------------------------------------------------------------------------
# support_client wiring
# ---------------------------------------------------------------------------


def test_unconfigured_integration_returns_503(monkeypatch):
    """Silence would look to the sender like a broadcast that worked."""
    monkeypatch.delenv("SUPPORT_API_BASE", raising=False)
    monkeypatch.delenv("SUPPORT_SERVICE_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        support_client.call("GET", "/telegram/groups", actor_email="a@b.c")
    assert exc.value.status_code == 503


def test_outbound_key_is_not_the_inbound_one(monkeypatch):
    """SUPPORT_API_KEY is the key Support presents when calling US. Reusing it
    for our outbound calls would hand our inbound credential to a third party,
    so the client must ignore it entirely."""
    monkeypatch.setenv("SUPPORT_API_BASE", "https://support.example")
    monkeypatch.setenv("SUPPORT_API_KEY", "inbound-secret")
    monkeypatch.delenv("SUPPORT_SERVICE_API_KEY", raising=False)
    with pytest.raises(HTTPException) as exc:
        support_client.call("GET", "/telegram/groups", actor_email="a@b.c")
    assert exc.value.status_code == 503


def test_call_sends_the_key_and_the_acting_user(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_BASE", "https://support.example/")
    monkeypatch.setenv("SUPPORT_SERVICE_API_KEY", "svc-key")
    captured = {}

    class _Response:
        status_code = 200
        content = b'{"ok": true}'

        def json(self):
            return {"ok": True}

    def _fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Response()

    monkeypatch.setattr(support_client.requests, "request", _fake_request)
    result = support_client.call(
        "GET", "/telegram/groups", actor_email="head@x.kz", actor_name="Head"
    )

    assert result == {"ok": True}
    assert captured["url"] == "https://support.example/service-api/telegram/groups"
    assert captured["headers"]["X-API-Key"] == "svc-key"
    # Every announcement is attributed to a named human.
    assert captured["headers"]["X-Acting-User"] == "head@x.kz"


def test_support_error_detail_reaches_the_caller(monkeypatch):
    """The value of this screen is telling staff WHY a chat missed the message.
    A proxy that flattened errors to 502 would destroy exactly that."""
    monkeypatch.setenv("SUPPORT_API_BASE", "https://support.example")
    monkeypatch.setenv("SUPPORT_SERVICE_API_KEY", "svc-key")

    class _Response:
        status_code = 400
        content = b"{}"

        def json(self):
            return {"detail": "bot was kicked from the supergroup chat"}

    monkeypatch.setattr(
        support_client.requests, "request", lambda method, url, **kw: _Response()
    )
    with pytest.raises(HTTPException) as exc:
        support_client.call("POST", "/announcements", actor_email="a@b.c")

    assert exc.value.status_code == 400
    assert "kicked" in exc.value.detail


def test_unreachable_support_is_502_not_a_silent_success(monkeypatch):
    monkeypatch.setenv("SUPPORT_API_BASE", "https://support.example")
    monkeypatch.setenv("SUPPORT_SERVICE_API_KEY", "svc-key")

    def _boom(method, url, **kwargs):
        raise support_client.requests.RequestException("connection refused")

    monkeypatch.setattr(support_client.requests, "request", _boom)
    with pytest.raises(HTTPException) as exc:
        support_client.call("GET", "/announcements", actor_email="a@b.c")
    assert exc.value.status_code == 502


# ---------------------------------------------------------------------------
# Request validation done before anything crosses the network
# ---------------------------------------------------------------------------


def test_malformed_payload_is_rejected_locally():
    with pytest.raises(HTTPException) as exc:
        routes.create_announcement(payload="not json", images=[], current_user=_user("admin"))
    assert exc.value.status_code == 422


def test_more_than_ten_images_is_rejected_before_upload(monkeypatch):
    """Telegram's album cap is 10. Rejecting here saves the user a pointless
    upload of every image across two networks."""
    images = [SimpleNamespace(filename=f"p{i}.png", file=None, content_type="image/png")
              for i in range(11)]
    with pytest.raises(HTTPException) as exc:
        routes.create_announcement(
            payload=json.dumps({"body": "x"}), images=images, current_user=_user("admin")
        )
    assert exc.value.status_code == 422
    assert "10" in exc.value.detail


def test_group_status_must_be_one_of_the_known_values():
    with pytest.raises(HTTPException) as exc:
        routes.set_group_status(1, {"status": "deleted"}, current_user=_user("admin"))
    assert exc.value.status_code == 422


def test_test_send_requires_an_integer_chat_id():
    with pytest.raises(HTTPException) as exc:
        routes.test_send(1, {"chat_id": "@somechannel"}, current_user=_user("admin"))
    assert exc.value.status_code == 422
