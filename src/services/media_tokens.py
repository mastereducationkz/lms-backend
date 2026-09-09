"""Short-lived, prefix-scoped tokens for streaming private media.

Signed with the app's existing JWT secret so there is exactly one signing key in
the system. The token names a storage prefix rather than a single file because
one HLS playlist fans out into hundreds of segment requests; scoping to the
prefix lets all of them through while still confining the holder to one lesson's
video.

Because the secret and algorithm are shared with the access/refresh tokens, every
media token carries ``typ: media`` and the verifier insists on it. Without that
the only thing separating the token families is which claims each verifier
happens to look at, which is an accident, not a boundary.
"""
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from src.utils.auth_utils import ALGORITHM, SECRET_KEY

# Six hours. A lesson runs 60 minutes, but a student pauses, walks away and comes
# back, or rewatches; the frontend mints the token once when the step is opened,
# so a TTL equal to the lesson length expires mid-playback for anyone who does not
# watch straight through. Six hours covers a realistic sitting and is still
# nothing next to the unguessable-filename scheme it replaces, which never expired.
MEDIA_TOKEN_TTL_SECONDS = 21600

TOKEN_TYPE = "media"


def normalise_key(path: str) -> str:
    """Collapse ``.`` and ``..`` into a plain storage key, anchored at the root.

    This is the *only* normalisation in the video path. The ``/uploads/`` routes
    run an incoming request path through it once and hand the result to both the
    prefix check below and the storage fetch. Two normalisations that disagree —
    a guard that classifies ``materials/../videos/x`` by its first segment while
    the filesystem resolves it to ``videos/x`` — is exactly how a traversal walks
    past a guard, so there is one function and one call per request.
    """
    return posixpath.normpath("/" + path.strip("/")).lstrip("/")


def mint_media_token(prefix: str, user_id: int, ttl_seconds: int = MEDIA_TOKEN_TTL_SECONDS) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    return jwt.encode(
        {"typ": TOKEN_TYPE, "pfx": normalise_key(prefix), "uid": user_id, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def verify_media_token(token: str, path: str) -> Optional[dict]:
    """Return the payload if ``token`` authorises ``path``, else ``None``.

    ``exp`` is required rather than merely checked-if-present: a token without one
    would otherwise be an eternal one. ``typ`` is required so an access or refresh
    token — same secret, same algorithm — can never stand in for a media token.
    """
    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != TOKEN_TYPE:
        return None
    prefix = payload.get("pfx")
    if not prefix:
        return None
    candidate = normalise_key(path)
    if candidate != prefix and not candidate.startswith(prefix + "/"):
        return None
    return payload


def signed_hls_url(stored_path: Optional[str], user_id: int) -> Optional[str]:
    """Turn a stored ``/uploads/videos/<id>/<lang>/master.m3u8`` path into a
    token-bearing URL. The token is scoped to the playlist's directory so the
    variant playlists and segments alongside it resolve under the same token."""
    if not stored_path:
        return None
    key = normalise_key(stored_path.split("/uploads/", 1)[-1])
    prefix = posixpath.dirname(key)
    token = mint_media_token(prefix, user_id)
    return f"/uploads/v/{token}/{key}"
