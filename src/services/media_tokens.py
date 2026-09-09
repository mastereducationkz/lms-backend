"""Short-lived, prefix-scoped tokens for streaming private media.

Signed with the app's existing JWT secret so there is exactly one signing key in
the system. The token names a storage prefix rather than a single file because
one HLS playlist fans out into hundreds of segment requests; scoping to the
prefix lets all of them through while still confining the holder to one lesson's
video.
"""
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from src.utils.auth_utils import ALGORITHM, SECRET_KEY

MEDIA_TOKEN_TTL_SECONDS = 3600


def _normalise(path: str) -> str:
    """Collapse ``.`` and ``..`` so a traversal cannot escape the signed prefix."""
    return posixpath.normpath("/" + path.strip("/")).lstrip("/")


def mint_media_token(prefix: str, user_id: int, ttl_seconds: int = MEDIA_TOKEN_TTL_SECONDS) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return jwt.encode(
        {"pfx": _normalise(prefix), "uid": user_id, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def verify_media_token(token: str, path: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    prefix = payload.get("pfx")
    if not prefix:
        return None
    candidate = _normalise(path)
    if candidate != prefix and not candidate.startswith(prefix + "/"):
        return None
    return payload


def signed_hls_url(stored_path: Optional[str], user_id: int) -> Optional[str]:
    """Turn a stored ``/uploads/videos/<id>/<lang>/master.m3u8`` path into a
    token-bearing URL. The token is scoped to the playlist's directory so the
    variant playlists and segments alongside it resolve under the same token."""
    if not stored_path:
        return None
    key = _normalise(stored_path.split("/uploads/", 1)[-1])
    prefix = posixpath.dirname(key)
    token = mint_media_token(prefix, user_id)
    return f"/uploads/v/{token}/{key}"
