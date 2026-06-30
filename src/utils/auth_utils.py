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
