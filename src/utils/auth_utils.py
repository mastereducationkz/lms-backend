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
