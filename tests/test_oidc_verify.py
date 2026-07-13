"""OIDC relying-party verifier (SSO Phase 2). A locally generated RSA key stands
in for the IdP, so these exercise the full RS256/JWKS/issuer/audience validation
with no network and no running Zitadel."""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.utils import oidc

ISSUER = "https://id.mastereducation.kz"
AUD = "lms"
KID = "test-kid-1"


def _make_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    pub_jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return private_pem, {"keys": [pub_jwk]}


PRIVATE_PEM, JWKS = _make_key()


def _token(private_pem=None, *, iss=ISSUER, aud=AUD, kid=KID, exp_delta=300, drop_sub=False, alg="RS256", claims=None):
    now = int(time.time())
    payload = {"iss": iss, "aud": aud, "sub": "idp-user-123", "email": "t@x.com", "iat": now, "exp": now + exp_delta}
    if drop_sub:
        payload.pop("sub")
    if claims:
        payload.update(claims)
    key = private_pem if private_pem is not None else PRIVATE_PEM
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


def _verify(token):
    return oidc.verify_oidc_claims(token, jwks=JWKS, issuers=[ISSUER], audience=AUD, leeway=30)


def test_valid_token_verifies():
    claims = _verify(_token())
    assert claims["sub"] == "idp-user-123"
    assert claims["email"] == "t@x.com"


def test_untrusted_issuer_rejected():
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(iss="https://evil.example.com"))


def test_wrong_audience_rejected():
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(aud="crm"))


def test_expired_rejected():
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(exp_delta=-3600))


def test_missing_sub_rejected():
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(drop_sub=True))


def test_unknown_kid_rejected():
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(kid="some-other-kid"))


def test_tampered_signature_rejected():
    tok = _token()
    header, payload, sig = tok.split(".")
    with pytest.raises(oidc.OidcVerifyError):
        _verify(f"{header}.{payload}.{sig[:-3]}xyz")


def test_hs256_alg_confusion_rejected():
    # An HS256 token (even with a valid-looking payload) must be rejected up front.
    hs = jwt.encode({"iss": ISSUER, "aud": AUD, "sub": "x", "iat": int(time.time()), "exp": int(time.time()) + 300},
                    "some-shared-secret", algorithm="HS256", headers={"kid": KID})
    with pytest.raises(oidc.OidcVerifyError):
        _verify(hs)


def test_key_from_different_issuer_key_rejected():
    # A token signed by a DIFFERENT RSA key (not in our JWKS) must fail.
    other_pem, _ = _make_key()
    with pytest.raises(oidc.OidcVerifyError):
        _verify(_token(other_pem))


def test_no_issuers_configured_rejects():
    with pytest.raises(oidc.OidcVerifyError):
        oidc.verify_oidc_claims(_token(), jwks=JWKS, issuers=[], audience=AUD)


# --- config helpers + outer verify_oidc_token (JWKS fetch mocked) ---

def test_flags_default_off(monkeypatch):
    monkeypatch.delenv("OIDC_ACCEPT", raising=False)
    assert oidc.oidc_accept_enabled() is False
    monkeypatch.setenv("OIDC_ACCEPT", "true")
    assert oidc.oidc_accept_enabled() is True


def test_verify_oidc_token_requires_config(monkeypatch):
    monkeypatch.delenv("OIDC_ISSUERS", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    with pytest.raises(oidc.OidcConfigError):
        oidc.verify_oidc_token(_token())


def test_verify_oidc_token_end_to_end_with_mocked_jwks(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUERS", ISSUER)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://id.mastereducation.kz/.well-known/jwks.json")
    monkeypatch.setenv("OIDC_AUDIENCE", AUD)
    oidc.clear_jwks_cache()
    monkeypatch.setattr(oidc, "_fetch_jwks", lambda url, **kw: JWKS)
    claims = oidc.verify_oidc_token(_token())
    assert claims["sub"] == "idp-user-123"


class _JwksResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_jwks_stale_while_revalidate(monkeypatch):
    """A failing JWKS refresh must serve the last-known-good keys, not lock every SSO
    login out — the production bug: a 5s timeout to the EU JWKS endpoint made every
    token 'fail validation' and bounced students back to login."""
    url = "https://id.mastereducation.kz/.well-known/jwks.json"
    oidc.clear_jwks_cache()

    # 1) first fetch succeeds and populates the cache
    monkeypatch.setattr(oidc.httpx, "get", lambda u, **kw: _JwksResp(JWKS))
    assert oidc._fetch_jwks(url) == JWKS

    # 2) force the cache stale, then make the network time out
    oidc._jwks_cache[url] = (0.0, JWKS)  # epoch timestamp => treated as expired

    def _boom(u, **kw):
        raise oidc.httpx.ConnectTimeout("simulated JWKS timeout")

    monkeypatch.setattr(oidc.httpx, "get", _boom)
    # stale-while-revalidate: returns the cached keys instead of raising
    assert oidc._fetch_jwks(url) == JWKS


def test_jwks_fails_closed_when_never_fetched(monkeypatch):
    """With no cached keys, a failing fetch must fail closed (raise), never accept blindly."""
    url = "https://id.mastereducation.kz/.well-known/jwks-cold.json"
    oidc.clear_jwks_cache()

    def _boom(u, **kw):
        raise oidc.httpx.ConnectTimeout("simulated JWKS timeout")

    monkeypatch.setattr(oidc.httpx, "get", _boom)
    with pytest.raises(oidc.OidcConfigError):
        oidc._fetch_jwks(url)
