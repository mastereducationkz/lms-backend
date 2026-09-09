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
from src.services.media_tokens import mint_media_token, verify_media_token


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
