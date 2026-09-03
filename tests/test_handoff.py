"""Session handoff links (Platform Integration Pack §3): the LMS mints a 60-second RS256 token
the platform redeems; public keys are published as a JWKS."""

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from src.integrations import handoff
from src.integrations.handoff_routes import handoff_router, wellknown_router
from src.routes.auth import get_current_user_dependency


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@pytest.fixture(autouse=True)
def env(monkeypatch, keypair):
    pem, _ = keypair
    monkeypatch.setenv("HANDOFF_ENABLED", "true")
    monkeypatch.setenv("HANDOFF_PRIVATE_KEY_PEM", pem)
    monkeypatch.delenv("HANDOFF_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("HANDOFF_KEY_ID", "k1")
    monkeypatch.setenv("IELTS_PLATFORM_URL", "https://ielts.example")
    monkeypatch.setenv("SAT_PLATFORM_URL", "https://sat.example")
    monkeypatch.setattr(handoff, "_redis_client", lambda: None)  # in-memory rate limit
    handoff.reset_caches()
    yield
    handoff.reset_caches()


def _user(role="student", subject="30293", uid=7, email="stu@x.io"):
    return SimpleNamespace(id=uid, role=role, email=email, central_auth_user_id=subject, is_active=True)


# --- role + return_to rules ------------------------------------------------------

@pytest.mark.parametrize("lms_role, expected", [
    ("student", "student"), ("teacher", "teacher"), ("head_teacher", "teacher"),
    ("curator", "curator"), ("head_curator", "curator"), ("admin", "admin"), ("parent", None),
])
def test_token_role_mapping(lms_role, expected):
    assert handoff.token_role(lms_role) == expected


@pytest.mark.parametrize("path", [
    "/", "/dashboard", "/weekly-sets", "/weekly-sets/7", "/stats?tab=1", "/writing", "/writing-practice",
    "/speaking-ai", "/exam/test/5", "/writing/task/2", "/exam/result/3304", "/writing/result/555",
    "/writing-practice/result/1", "/speaking-ai/result/9",
])
def test_student_allowlist_accepts(path):
    assert handoff.validate_return_to(path, "student") == path


@pytest.mark.parametrize("path", ["/admin", "/exam", "/users/1", "/dashboardx", "/weekly-setsx/1"])
def test_student_allowlist_rejects_other_pages(path):
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.validate_return_to(path, "student")
    assert exc.value.status_code == 403


def test_staff_may_target_any_path():
    assert handoff.validate_return_to("/admin/anything?x=1", "teacher") == "/admin/anything?x=1"


@pytest.mark.parametrize("bad", ["", "evil", "//evil.example/x", "http://evil.example", "/x\ny", "/\\evil"])
def test_return_to_must_be_a_plain_path(bad):
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.validate_return_to(bad, "admin")
    assert exc.value.status_code == 400


# --- token -----------------------------------------------------------------------

def test_mint_token_claims_and_header(keypair):
    _, public = keypair
    before = int(time.time())

    token = handoff.mint_token(_user(), "ielts", "/exam/result/3304")

    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256" and header["kid"] == "k1"
    claims = jwt.decode(token, public, algorithms=["RS256"], audience="ielts", issuer="lms")
    assert claims["sub"] == "30293" and claims["email"] == "stu@x.io" and claims["role"] == "student"
    assert claims["purpose"] == "session_handoff" and claims["return_to"] == "/exam/result/3304"
    assert claims["exp"] - claims["iat"] == 60 and claims["nbf"] == claims["iat"]
    assert before <= claims["iat"] <= int(time.time())
    assert len(claims["jti"]) == 36


def test_sub_falls_back_to_lms_id_without_subject(keypair):
    _, public = keypair
    token = handoff.mint_token(_user(subject=None, uid=42), "sat", "/x")
    assert jwt.decode(token, public, algorithms=["RS256"], audience="sat")["sub"] == "lms:42"


def test_jwks_publishes_the_verifying_key():
    jwks = handoff.build_jwks()
    (key,) = jwks["keys"]
    assert key["kty"] == "RSA" and key["use"] == "sig" and key["alg"] == "RS256" and key["kid"] == "k1"
    assert "d" not in key                                  # never the private part
    public = RSAAlgorithm.from_jwk(json.dumps(key))
    token = handoff.mint_token(_user(), "ielts", "/")
    assert jwt.decode(token, public, algorithms=["RS256"], audience="ielts")["iss"] == "lms"


def test_private_key_can_come_from_a_file(monkeypatch, tmp_path, keypair):
    pem, public = keypair
    path = tmp_path / "handoff.pem"
    path.write_text(pem)
    monkeypatch.delenv("HANDOFF_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.setenv("HANDOFF_PRIVATE_KEY_PATH", str(path))
    handoff.reset_caches()
    token = handoff.mint_token(_user(), "ielts", "/")
    assert jwt.decode(token, public, algorithms=["RS256"], audience="ielts")["purpose"] == "session_handoff"


def test_missing_key_is_a_503(monkeypatch):
    monkeypatch.delenv("HANDOFF_PRIVATE_KEY_PEM", raising=False)
    handoff.reset_caches()
    with pytest.raises(handoff.HandoffError) as exc:
        handoff.mint_token(_user(), "ielts", "/")
    assert exc.value.status_code == 503


# --- HTTP ------------------------------------------------------------------------

@pytest.fixture()
def make_client():
    def _make(user):
        app = FastAPI()
        app.include_router(handoff_router, prefix="/handoff")
        app.include_router(wellknown_router)
        app.dependency_overrides[get_current_user_dependency] = lambda: user
        return TestClient(app)
    return _make


def test_mint_returns_handoff_url(make_client, keypair):
    _, public = keypair
    resp = make_client(_user()).post("/handoff/mint", json={"platform": "ielts", "return_to": "/exam/result/3304"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 60
    assert body["url"].startswith("https://ielts.example/auth/handoff?token=")
    token = body["url"].split("token=", 1)[1]
    assert jwt.decode(token, public, algorithms=["RS256"], audience="ielts")["return_to"] == "/exam/result/3304"


def test_mint_503_when_disabled(make_client, monkeypatch):
    monkeypatch.setenv("HANDOFF_ENABLED", "false")
    resp = make_client(_user()).post("/handoff/mint", json={"platform": "ielts", "return_to": "/"})
    assert resp.status_code == 503


def test_mint_403_for_parent(make_client):
    resp = make_client(_user(role="parent")).post("/handoff/mint", json={"platform": "ielts", "return_to": "/"})
    assert resp.status_code == 403


def test_mint_403_student_outside_allowlist(make_client):
    resp = make_client(_user()).post("/handoff/mint", json={"platform": "ielts", "return_to": "/admin"})
    assert resp.status_code == 403


def test_mint_400_bad_return_to_and_platform(make_client):
    client = make_client(_user())
    assert client.post("/handoff/mint", json={"platform": "ielts", "return_to": "//evil"}).status_code == 400
    assert client.post("/handoff/mint", json={"platform": "toefl", "return_to": "/"}).status_code == 400


def test_mint_rate_limited_per_user(make_client):
    client = make_client(_user(uid=99))
    for _ in range(30):
        assert client.post("/handoff/mint", json={"platform": "ielts", "return_to": "/"}).status_code == 200
    assert client.post("/handoff/mint", json={"platform": "ielts", "return_to": "/"}).status_code == 429
    # another user is unaffected
    assert make_client(_user(uid=100)).post("/handoff/mint", json={"platform": "ielts", "return_to": "/"}).status_code == 200


def test_jwks_endpoint(make_client):
    resp = make_client(_user()).get("/.well-known/handoff-jwks.json")
    assert resp.status_code == 200
    assert resp.json()["keys"][0]["kid"] == "k1"
    assert "max-age=600" in resp.headers["cache-control"]


def test_jwks_503_without_key(make_client, monkeypatch):
    monkeypatch.delenv("HANDOFF_PRIVATE_KEY_PEM", raising=False)
    handoff.reset_caches()
    assert make_client(_user()).get("/.well-known/handoff-jwks.json").status_code == 503
