"""verify_bearer_token dual-run seam (SSO Phase 2): legacy HS256 first, OIDC fallback.

Confirms the flag-off no-op guarantee (OIDC is never attempted unless OIDC_ACCEPT
is set, and a legacy HS256 token always wins) and the OIDC mapping (verified IdP
token -> {sub: lower(email)}) so the existing email-based user lookup works."""

from src.utils import auth_utils
from src.utils import oidc


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
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-123", "email": "Teacher@X.com"})
    p = auth_utils.verify_bearer_token("oidc.token.here")
    assert p == {
        "sub": "teacher@x.com",
        "email": "teacher@x.com",
        "oidc": True,
        "central_auth_user_id": "idp-123",
    }


def test_oidc_on_but_no_email_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    monkeypatch.setattr(oidc, "verify_oidc_token", lambda t: {"sub": "idp-123"})
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
