"""Phase-0 teacher-login handoff: LMS as credential authority.

Pure-decision unit tests (no DB / no network), mirroring the sibling
``test_crm_internal_send_invite.py`` style. The password-scheme parity is what
guarantees no existing teacher login regresses when the flag is enabled.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import bcrypt
import pytest

from src.routes import crm_internal
from src.services import sso_broker
from src.utils import auth_utils


ROLES = ("teacher", "head_teacher")


def _teacher(role="teacher", is_active=True, password="Secret123", scheme="bcrypt", email="t@x.com"):
    if scheme == "bcrypt":
        stored = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    elif scheme == "plain":
        stored = password
    else:
        stored = scheme  # caller passed a literal stored value
    return SimpleNamespace(id=7, email=email, name="Teacher", role=role, is_active=is_active, hashed_password=stored)


# --- password-scheme parity (verify_lms_stored_password) ---------------------

def test_verify_accepts_bcrypt():
    stored = bcrypt.hashpw(b"Secret123", bcrypt.gensalt()).decode()
    assert auth_utils.verify_lms_stored_password("Secret123", stored) is True
    assert auth_utils.verify_lms_stored_password("wrong", stored) is False


def test_verify_accepts_django_pbkdf2():
    # Django default format: pbkdf2_sha256$iterations$salt$b64hash
    import base64, hashlib
    salt, iters = "abc123salt", 260000
    derived = hashlib.pbkdf2_hmac("sha256", b"Secret123", salt.encode(), iters)
    stored = f"pbkdf2_sha256${iters}${salt}${base64.b64encode(derived).decode()}"
    assert auth_utils.verify_lms_stored_password("Secret123", stored) is True
    assert auth_utils.verify_lms_stored_password("nope", stored) is False


def test_verify_accepts_plaintext_legacy():
    assert auth_utils.verify_lms_stored_password("Secret123", "Secret123") is True
    assert auth_utils.verify_lms_stored_password("Secret123", "other") is False


def test_verify_rejects_empty_stored():
    assert auth_utils.verify_lms_stored_password("x", None) is False
    assert auth_utils.verify_lms_stored_password("x", "") is False


# --- authorization decision (_teacher_handoff_authorized) --------------------

def test_authorized_for_active_teacher_correct_password():
    assert crm_internal._teacher_handoff_authorized(_teacher(), "Secret123", ROLES) is True


def test_rejected_wrong_password():
    assert crm_internal._teacher_handoff_authorized(_teacher(), "wrong", ROLES) is False


def test_rejected_inactive_teacher():
    assert crm_internal._teacher_handoff_authorized(_teacher(is_active=False), "Secret123", ROLES) is False


def test_rejected_non_teacher_role():
    assert crm_internal._teacher_handoff_authorized(_teacher(role="student"), "Secret123", ROLES) is False


def test_rejected_missing_user():
    assert crm_internal._teacher_handoff_authorized(None, "Secret123", ROLES) is False


def test_head_teacher_allowed():
    assert crm_internal._teacher_handoff_authorized(_teacher(role="head_teacher"), "Secret123", ROLES) is True


# --- mint payload shape (_teacher_handoff_mint_kwargs) -----------------------

def test_mint_kwargs_shape():
    kwargs = crm_internal._teacher_handoff_mint_kwargs(_teacher())
    assert kwargs["sub"] == "lms:7"
    assert kwargs["aud"] == "crm"
    assert kwargs["scope"] == ["crm:teacher"]
    assert kwargs["email"] == "t@x.com"
    assert kwargs["name"] == "Teacher"


# --- flag gating: disabled → 503 (dual-run guarantee) ------------------------

def test_endpoint_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("SSO_BROKER_ENABLED", raising=False)
    assert crm_internal.sso_broker_enabled() is False


def test_endpoint_enabled_with_flag(monkeypatch):
    monkeypatch.setenv("SSO_BROKER_ENABLED", "true")
    assert crm_internal.sso_broker_enabled() is True


# --- broker mint client robustness (bad/incomplete 200 bodies -> SsoBrokerError, not 500) ---

class _FakeResp:
    def __init__(self, payload=None, raise_json=False):
        self._payload = payload
        self._raise_json = raise_json

    def raise_for_status(self):
        return None

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def test_mint_handoff_raises_on_non_json(monkeypatch):
    monkeypatch.setenv("SSO_BROKER_URL", "https://broker.test")
    monkeypatch.setattr(sso_broker.httpx, "post", lambda *a, **k: _FakeResp(raise_json=True))
    with pytest.raises(sso_broker.SsoBrokerError):
        sso_broker.mint_handoff(sub="lms:1", aud="crm", scope=[])


def test_mint_handoff_raises_on_missing_keys(monkeypatch):
    monkeypatch.setenv("SSO_BROKER_URL", "https://broker.test")
    monkeypatch.setattr(sso_broker.httpx, "post", lambda *a, **k: _FakeResp(payload={"jti": "x"}))
    with pytest.raises(sso_broker.SsoBrokerError):
        sso_broker.mint_handoff(sub="lms:1", aud="crm", scope=[])


def test_mint_handoff_ok(monkeypatch):
    monkeypatch.setenv("SSO_BROKER_URL", "https://broker.test")
    monkeypatch.setattr(
        sso_broker.httpx, "post",
        lambda *a, **k: _FakeResp(payload={"handoff": "h.e.re", "jti": "x", "expires_at": 123}),
    )
    out = sso_broker.mint_handoff(sub="lms:1", aud="crm", scope=["crm:teacher"])
    assert out["handoff"] == "h.e.re"
