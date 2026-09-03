"""Session handoff links (Platform Integration Pack §3).

The LMS mints a short-lived RS256 JWT that a platform (IELTS, later SAT) redeems for a local
session, so a student clicking "open my result" lands on the platform already signed in.
Public keys are published at ``/.well-known/handoff-jwks.json`` (see handoff_routes).

Flag ``HANDOFF_ENABLED`` (off by default → 503, clients fall back to the bare platform host).
Key: ``HANDOFF_PRIVATE_KEY_PATH`` (a PEM file mounted into the container) or
``HANDOFF_PRIVATE_KEY_PEM`` (inline, ``\\n`` escapes accepted); ``HANDOFF_KEY_ID`` is the ``kid``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Optional

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}
PLATFORMS = ("ielts", "sat")
TOKEN_TTL_SECONDS = 60
RATE_LIMIT_PER_MINUTE = 30
_RATE_WINDOW_SECONDS = 60
ISSUER = "lms"
PURPOSE = "session_handoff"

_DEFAULT_PLATFORM_URLS = {
    "ielts": "https://ielts.mastereducation.kz",
    "sat": "https://sat.mastereducation.kz",
}

# LMS role -> token role. Parents have no platform account (None => 403).
_ROLE_MAP = {
    "student": "student",
    "teacher": "teacher",
    "head_teacher": "teacher",
    "curator": "curator",
    "head_curator": "curator",
    "admin": "admin",
}

# Students may only be handed to their own pages; staff may target any path.
STUDENT_RETURN_TO_PREFIXES = (
    "/dashboard", "/weekly-sets", "/stats", "/writing", "/writing-practice", "/speaking-ai",
    "/exam/test", "/writing/task",
    "/exam/result", "/writing/result", "/writing-practice/result", "/speaking-ai/result",
)


class HandoffError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def handoff_enabled() -> bool:
    return os.getenv("HANDOFF_ENABLED", "").strip().lower() in _TRUTHY


def platform_base_url(platform: str) -> str:
    return (os.getenv(f"{platform.upper()}_PLATFORM_URL", "").strip() or _DEFAULT_PLATFORM_URLS[platform]).rstrip("/")


def token_role(lms_role: Optional[str]) -> Optional[str]:
    return _ROLE_MAP.get((lms_role or "").strip().lower())


def validate_return_to(return_to: str, role: str) -> str:
    """A platform-relative path only (never a URL, never protocol-relative), and for students
    one of the allow-listed pages."""
    if not isinstance(return_to, str) or not return_to.startswith("/") or return_to.startswith("//"):
        raise HandoffError(400, "return_to must be a path on the platform")
    if "\\" in return_to or any(ch.isspace() or ord(ch) < 32 for ch in return_to):
        raise HandoffError(400, "return_to must be a path on the platform")
    if role == "student":
        path = return_to.split("?", 1)[0].split("#", 1)[0]
        allowed = path == "/" or any(
            path == prefix or path.startswith(prefix + "/") for prefix in STUDENT_RETURN_TO_PREFIXES
        )
        if not allowed:
            raise HandoffError(403, "students may only open their own platform pages")
    return return_to


# --- signing key -----------------------------------------------------------------

_key_lock = threading.Lock()
_key_cache: Optional[tuple[rsa.RSAPrivateKey, str]] = None


def _read_private_pem() -> str:
    inline = os.getenv("HANDOFF_PRIVATE_KEY_PEM", "")
    if inline.strip():
        return inline.replace("\\n", "\n")
    path = os.getenv("HANDOFF_PRIVATE_KEY_PATH", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            logger.error("handoff: cannot read HANDOFF_PRIVATE_KEY_PATH %s: %s", path, exc)
    return ""


def load_private_key() -> tuple[rsa.RSAPrivateKey, str]:
    """The RSA signing key and its ``kid``; cached after the first successful load."""
    global _key_cache
    with _key_lock:
        if _key_cache is not None:
            return _key_cache
        pem = _read_private_pem()
        if not pem.strip():
            raise HandoffError(503, "handoff signing key is not configured")
        try:
            key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
        except (ValueError, TypeError) as exc:
            raise HandoffError(503, "handoff signing key is not a valid PEM private key") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise HandoffError(503, "handoff signing key must be RSA")
        kid = os.getenv("HANDOFF_KEY_ID", "").strip() or "lms-handoff"
        _key_cache = (key, kid)
        return _key_cache


def reset_caches() -> None:
    """Tests and key rotation: forget the loaded key and the in-memory rate-limit buckets."""
    global _key_cache
    with _key_lock:
        _key_cache = None
    with _rl_lock:
        _rl_buckets.clear()


def build_jwks() -> dict:
    key, kid = load_private_key()
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"use": "sig", "alg": "RS256", "kid": kid})
    return {"keys": [jwk]}


# --- minting ----------------------------------------------------------------------

def mint_token(user, platform: str, return_to: str, *, now: Optional[float] = None) -> str:
    key, kid = load_private_key()
    role = token_role(getattr(user, "role", None))
    if role is None:
        raise HandoffError(403, "this account has no platform access")
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": ISSUER,
        "aud": platform,
        "sub": getattr(user, "central_auth_user_id", None) or f"lms:{user.id}",
        "email": (getattr(user, "email", "") or "").strip().lower(),
        "role": role,
        "purpose": PURPOSE,
        "return_to": return_to,
        "jti": str(uuid.uuid4()),
        "iat": issued,
        "nbf": issued,
        "exp": issued + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def mint_handoff(user, platform: str, return_to: str) -> dict:
    """The full request path: flag → platform → role → return_to → rate limit → token."""
    if not handoff_enabled():
        raise HandoffError(503, "handoff is disabled")
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise HandoffError(400, "unknown platform")
    role = token_role(getattr(user, "role", None))
    if role is None:
        raise HandoffError(403, "this account has no platform access")
    return_to = validate_return_to(return_to, role)
    check_rate_limit(user.id)
    token = mint_token(user, platform, return_to)
    return {"url": f"{platform_base_url(platform)}/auth/handoff?token={token}", "expires_in": TOKEN_TTL_SECONDS}


# --- rate limit (30/min/user): Redis when available, else per-process memory ------

_rl_lock = threading.Lock()
_rl_buckets: dict[int, tuple[int, int]] = {}  # user_id -> (window_start, count)


def _redis_client():
    try:
        from src.services import cache_service

        return cache_service.get_client()
    except Exception:  # noqa: BLE001 - cache is optional everywhere in this app
        return None


def check_rate_limit(user_id: int, *, limit: int = RATE_LIMIT_PER_MINUTE, now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    window = int(now // _RATE_WINDOW_SECONDS)
    client = _redis_client()
    if client is not None:
        try:
            key = f"handoff:rl:{user_id}:{window}"
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, _RATE_WINDOW_SECONDS * 2)
            if count > limit:
                raise HandoffError(429, "too many handoff requests, try again in a minute")
            return
        except HandoffError:
            raise
        except Exception as exc:  # noqa: BLE001 - degrade to memory on a Redis hiccup
            logger.warning("handoff rate limit: redis unavailable (%s), using memory", exc)
    with _rl_lock:
        start, count = _rl_buckets.get(user_id, (window, 0))
        if start != window:
            start, count = window, 0
        count += 1
        _rl_buckets[user_id] = (start, count)
    if count > limit:
        raise HandoffError(429, "too many handoff requests, try again in a minute")
