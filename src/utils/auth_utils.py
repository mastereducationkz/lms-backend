import base64
import hmac
import os
import hashlib
import logging
import jwt
from datetime import datetime, timedelta
from typing import Optional
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

_logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not SECRET_KEY:
    if os.getenv("ENVIRONMENT") == "production":
        raise RuntimeError(
            "JWT_SECRET_KEY must be set in environment for production. "
            "Generate: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    SECRET_KEY = "dev-only-change-in-production"
    _logger.warning("JWT_SECRET_KEY not set; using dev default. Set JWT_SECRET_KEY in production.")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 30  # 30 days

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password, returning False if hash format is unknown."""
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except UnknownHashError:
        # Stored hash format not recognized – treat as invalid credentials
        return False

def _verify_django_pbkdf2_sha256(plain_password: str, encoded_password: str) -> bool:
    """Verify a Django-style ``pbkdf2_sha256$iterations$salt$hash``."""
    try:
        algorithm, iterations_raw, salt, digest = encoded_password.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), int(iterations_raw)
        )
        return hmac.compare_digest(base64.b64encode(derived).decode("utf-8").strip(), digest)
    except Exception:
        return False


def verify_lms_stored_password(
    plain_password: str, stored_password: Optional[str], *, algorithm: str = "auto"
) -> bool:
    """Verify a password against an LMS ``users`` hash, tolerating legacy schemes.

    LMS is the credential authority for the SSO handoff flow, so it must accept
    exactly the hash formats that already exist in its ``users`` table. In ``auto``
    mode this detects bcrypt (``$2*``), Django pbkdf2-sha256, and finally a
    plaintext-at-rest fallback (a known legacy state the SSO migration will retire
    — see SSO_DESIGN.md §E). This preserves parity with the CRM teacher-login path
    it replaces, so no existing teacher login regresses when the flag is enabled.
    """
    if not stored_password:
        return False
    mode = (algorithm or "auto").strip().lower()
    if mode in ("auto", "bcrypt") and stored_password[:4] in ("$2a$", "$2b$", "$2y$"):
        return verify_password(plain_password, stored_password)
    if mode in ("auto", "django_pbkdf2_sha256") and stored_password.startswith("pbkdf2_sha256$"):
        return _verify_django_pbkdf2_sha256(plain_password, stored_password)
    if mode in ("auto", "plain"):
        return hmac.compare_digest(plain_password, stored_password)
    if mode == "bcrypt":
        return verify_password(plain_password, stored_password)
    if mode == "django_pbkdf2_sha256":
        return _verify_django_pbkdf2_sha256(plain_password, stored_password)
    return False


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.InvalidTokenError:
        return None


def verify_bearer_token(token: str):
    """Verify a bearer access token, accepting legacy HS256 OR a central-IdP token.

    SSO Phase 2 dual-run seam. Tries the legacy HS256 token first (unchanged path);
    only if that fails AND ``OIDC_ACCEPT`` is enabled does it try to verify an IdP
    RS256 token via JWKS and normalize it to a legacy-shaped payload (``sub`` = the
    lower-cased email claim) so the existing "look the user up by email" resolution
    works untouched — the LMS user's role/permissions still come from the DB row.

    When ``OIDC_ACCEPT`` is unset this is byte-for-byte identical to ``verify_token``.
    Always fails closed to ``None`` (never raises) so the auth path can only reject,
    never 500. Note: this does NOT provision users — an OIDC token only authenticates
    an already-existing, active LMS user matched by email.
    """
    payload = verify_token(token)
    if payload is not None:
        return payload

    # Lazy import so a missing crypto backend or OIDC misconfig can never break HS256 auth.
    try:
        from src.utils.oidc import oidc_accept_enabled, verify_oidc_token
    except Exception:  # pragma: no cover - defensive
        return None
    if not oidc_accept_enabled():
        return None
    try:
        claims = verify_oidc_token(token)
    except Exception as exc:  # OidcVerifyError / OidcConfigError / anything → reject
        _logger.info("OIDC bearer token rejected: %s", exc)
        return None

    # Email + its verified flag come from the same source: the token claim if present,
    # otherwise the userinfo endpoint (Zitadel JWT access tokens don't carry email).
    from src.utils.oidc import email_is_verified, oidc_require_email_verified

    email = (claims.get("email") or "").strip().lower()
    email_verified = email_is_verified(claims.get("email_verified"))
    if not email:
        try:
            from src.utils.oidc import fetch_userinfo

            info = fetch_userinfo(token)
            email = (info.get("email") or "").strip().lower()
            email_verified = email_is_verified(info.get("email_verified"))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.info("OIDC userinfo email resolution failed: %s", exc)

    # Security: never trust an UNVERIFIED IdP email as an LMS match/link key. Without this,
    # an IdP account whose (unverified) email equals a victim's LMS email would resolve to —
    # and backfill its subject onto — the victim's account (see resolve_user_by_payload). We
    # drop the untrusted email; with no trusted email left the token cannot map to a user and
    # is rejected. All estate accounts are imported email-verified, so real logins are
    # unaffected, and a *changed* email is still verified, so subject-first resolution holds.
    if email and not email_verified and oidc_require_email_verified():
        _logger.warning("OIDC email %r is not verified; refusing to map it to an LMS user", email)
        email = ""

    if not email:
        _logger.warning("OIDC token verified but no trusted email (token or userinfo); cannot map to an LMS user")
        return None
    return {
        "sub": email,
        "email": email,
        "oidc": True,
        "central_auth_user_id": claims.get("sub"),
        "email_verified": email_verified,
    }


# Password reset tokens are short-lived signed JWTs (no DB column needed). The `pv`
# (password-version) stamp binds the token to the current password hash, so the token
# is automatically invalidated once the password changes (single-use behaviour).
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 60  # 1 hour


def _password_stamp(hashed_password: Optional[str]) -> str:
    return hashlib.sha256((hashed_password or "").encode()).hexdigest()[:16]


def create_password_reset_token(email: str, user_id: int, hashed_password: Optional[str]) -> str:
    expire = datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    to_encode = {
        "sub": email,
        "uid": user_id,
        "type": "password_reset",
        "pv": _password_stamp(hashed_password),
        "exp": expire,
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_password_reset_token(token: str) -> Optional[dict]:
    """Return {email, uid, pv} for a valid, unexpired reset token, else None."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != "password_reset":
        return None
    return {"email": payload.get("sub"), "uid": payload.get("uid"), "pv": payload.get("pv")}


def password_stamp_matches(token_pv: Optional[str], hashed_password: Optional[str]) -> bool:
    return bool(token_pv) and token_pv == _password_stamp(hashed_password)
