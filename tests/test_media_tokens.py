"""A media token authorises one storage prefix, for one viewer, for a while.

The token rides in the URL path rather than the query string because HLS
playlists reference their segments relatively: a master playlist served from
``/uploads/v/<token>/videos/42/ru/master.m3u8`` yields segment requests under
the same ``/v/<token>/`` prefix without the player having to know the token
exists. A query string would be dropped by that relative resolution and every
segment would arrive unauthenticated.

The prefix check is a path-boundary check, not a string prefix check —
``videos/4`` must not open ``videos/42``.
"""
from datetime import datetime, timedelta, timezone

import jwt

from src.services.media_tokens import mint_media_token, verify_media_token
from src.utils.auth_utils import ALGORITHM, SECRET_KEY


def test_token_authorises_its_own_prefix():
    token = mint_media_token("videos/42/ru", user_id=7)
    payload = verify_media_token(token, "videos/42/ru/master.m3u8")
    assert payload is not None
    assert payload["uid"] == 7


def test_token_authorises_nested_segments():
    token = mint_media_token("videos/42/ru", user_id=7)
    assert verify_media_token(token, "videos/42/ru/v0_003.ts") is not None


def test_token_rejects_a_different_prefix():
    token = mint_media_token("videos/42/ru", user_id=7)
    assert verify_media_token(token, "videos/43/ru/master.m3u8") is None


def test_token_rejects_a_sibling_that_merely_shares_a_string_prefix():
    token = mint_media_token("videos/4", user_id=7)
    assert verify_media_token(token, "videos/42/ru/master.m3u8") is None


def test_token_rejects_traversal_out_of_its_prefix():
    token = mint_media_token("videos/42/ru", user_id=7)
    assert verify_media_token(token, "videos/42/ru/../../43/ru/master.m3u8") is None


def test_expired_token_is_rejected():
    token = mint_media_token("videos/42/ru", user_id=7, ttl_seconds=-1)
    assert verify_media_token(token, "videos/42/ru/master.m3u8") is None


def test_garbage_token_is_rejected():
    assert verify_media_token("not-a-token", "videos/42/ru/master.m3u8") is None


def _forge(**claims) -> str:
    """A token signed with the real key — what an attacker holding another token
    family's payload, or a future caller taking a shortcut, would present."""
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def test_a_token_of_another_type_is_rejected():
    """Media tokens share the secret and the algorithm with the access and refresh
    tokens. Only the ``typ`` claim separates the families; without it they are
    told apart by nothing more than which claims each verifier happens to read."""
    forged = _forge(
        typ="access",
        pfx="videos/42/ru",
        uid=7,
        exp=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert verify_media_token(forged, "videos/42/ru/master.m3u8") is None


def test_a_token_without_a_type_claim_is_rejected():
    forged = _forge(
        pfx="videos/42/ru", uid=7, exp=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    assert verify_media_token(forged, "videos/42/ru/master.m3u8") is None


def test_a_token_without_an_expiry_is_rejected():
    """A missing ``exp`` is not an absent restriction, it is an eternal token."""
    assert verify_media_token(_forge(typ="media", pfx="videos/42/ru", uid=7), "videos/42/ru/master.m3u8") is None


def test_a_minted_token_outlasts_a_lesson():
    """Lessons run 60 minutes and the frontend mints once, when the step opens. A
    TTL equal to the lesson expires on anyone who pauses or rewatches."""
    payload = jwt.decode(
        mint_media_token("videos/42/ru", user_id=7), SECRET_KEY, algorithms=[ALGORITHM]
    )
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert remaining > 3600
