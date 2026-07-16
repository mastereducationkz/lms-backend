"""verify_bearer_token dual-run seam (SSO Phase 2): legacy HS256 first, OIDC fallback.

Confirms the flag-off no-op guarantee (OIDC is never attempted unless OIDC_ACCEPT
is set, and a legacy HS256 token always wins) and the OIDC mapping (verified IdP
token -> {sub: lower(email)}) so the existing email-based user lookup works."""

import pytest

from src.utils import auth_utils
from src.utils import oidc


@pytest.fixture(autouse=True)
def _clear_oidc_payload_cache():
    # Tests reuse the same token strings; the per-worker positive-result cache
    # would otherwise leak an accepted payload into a rejection test.
    auth_utils._oidc_payload_cache.clear()
    yield
    auth_utils._oidc_payload_cache.clear()


def test_hs256_token_returned_unchanged(monkeypatch):
    monkeypatch.delenv("OIDC_ACCEPT", raising=False)
    tok = auth_utils.create_access_token({"sub": "a@b.com", "user_id": 1, "role": "student"})
    p = auth_utils.verify_bearer_token(tok)
    assert p["sub"] == "a@b.com"
    assert "oidc" not in p


def test_oidc_off_rejects_non_hs256_token(monkeypatch):
    monkeypatch.delenv("OIDC_ACCEPT", raising=False)
    # A non-HS256 token with the flag off must be rejected (OIDC path not taken).
    assert auth_utils.verify_bearer_token("not.a.valid.hs256") is None


def test_oidc_on_maps_email_lowercased(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(
        oidc, "verify_oidc_token",
        lambda t: {"sub": "idp-123", "email": "Teacher@X.com", "email_verified": True},
    )
    p = auth_utils.verify_bearer_token("oidc.token.here")
    assert p == {
        "sub": "teacher@x.com",
        "email": "teacher@x.com",
        "oidc": True,
        "central_auth_user_id": "idp-123",
        "email_verified": True,
    }


def test_oidc_on_but_no_email_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.delenv("OIDC_USERINFO_URL", raising=False)  # no userinfo fallback configured
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-123"})
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_oidc_email_resolved_from_userinfo_when_absent_from_token(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-9"})  # token has no email
    monkeypatch.setattr(
        oidc, "fetch_userinfo",
        lambda t, **k: {"email": "From@Userinfo.com", "email_verified": True},
    )
    p = auth_utils.verify_bearer_token("oidc.token")
    assert p["sub"] == "from@userinfo.com"
    assert p["central_auth_user_id"] == "idp-9"
    assert p["email_verified"] is True


def test_oidc_unverified_email_in_token_rejected(monkeypatch):
    # The security fix: an IdP email that isn't marked verified must not map to an LMS user,
    # even though it's a validly-signed token — it would otherwise hijack a victim by email.
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(
        oidc, "verify_oidc_token",
        lambda t: {"sub": "idp-123", "email": "victim@x.com", "email_verified": False},
    )
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_oidc_unverified_email_missing_flag_treated_as_unverified(monkeypatch):
    # No email_verified claim at all => not verified => rejected (fail-closed).
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.delenv("OIDC_USERINFO_URL", raising=False)
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-1", "email": "a@b.com"})
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_oidc_unverified_email_from_userinfo_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-9"})
    monkeypatch.setattr(
        oidc, "fetch_userinfo",
        lambda t, **k: {"email": "a@b.com", "email_verified": False},
    )
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_oidc_email_verified_accepts_stringy_true(monkeypatch):
    # Some IdPs serialize email_verified as the string "true"; accept it.
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(
        oidc, "verify_oidc_token",
        lambda t: {"sub": "idp-7", "email": "ok@x.com", "email_verified": "true"},
    )
    p = auth_utils.verify_bearer_token("oidc.token")
    assert p["sub"] == "ok@x.com" and p["email_verified"] is True


def test_oidc_require_email_verified_off_is_break_glass(monkeypatch):
    # Operator escape hatch: with enforcement disabled, an unverified email is trusted again.
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setenv("OIDC_REQUIRE_EMAIL_VERIFIED", "false")
    monkeypatch.setattr(
        oidc, "verify_oidc_token",
        lambda t: {"sub": "idp-3", "email": "legacy@x.com", "email_verified": False},
    )
    p = auth_utils.verify_bearer_token("oidc.token")
    assert p["sub"] == "legacy@x.com"


def test_oidc_rejected_when_no_email_in_token_or_userinfo(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-9"})
    monkeypatch.setattr(oidc, "fetch_userinfo", lambda t, **k: {})
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_oidc_on_but_verify_raises_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")

    def boom(_):
        raise oidc.OidcVerifyError("bad token")

    monkeypatch.setattr(oidc, "verify_oidc_token", boom)
    assert auth_utils.verify_bearer_token("oidc.token") is None


def test_hs256_wins_and_oidc_not_attempted_when_flag_on(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    calls = {"n": 0}

    def spy(_):
        calls["n"] += 1
        return {"sub": "idp", "email": "x@y.z"}

    monkeypatch.setattr(oidc, "verify_oidc_token", spy)
    tok = auth_utils.create_access_token({"sub": "real@user.com"})
    p = auth_utils.verify_bearer_token(tok)
    assert p["sub"] == "real@user.com"
    assert calls["n"] == 0  # legacy token verified first; OIDC never called
