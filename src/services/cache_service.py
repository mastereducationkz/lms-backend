"""Redis-backed cache service with graceful fallback.

Design goals:
- Optional dependency: if ``REDIS_URL`` is not set or the server is unreachable,
  every cache call becomes a no-op and the caller still works correctly.
- Per-call TTL with sensible defaults.
- Pattern invalidation (``invalidate("courses:*")``) so mutations can drop
  whole families of keys without enumerating them.
- A ``@cached`` decorator for FastAPI route handlers that builds a key from
  the current user and selected request arguments.

The cache stores JSON-serializable Python objects. Pydantic models are
serialized via ``model_dump``; anything else falls back to ``jsonable_encoder``
from FastAPI to handle dates/UUIDs/etc.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import logging
import os
import threading
from typing import Any, Callable, Iterable, Optional

from fastapi.encoders import jsonable_encoder

try:
    import redis  # type: ignore
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

logger = logging.getLogger(__name__)

_REDIS_CLIENT: Optional["redis.Redis"] = None
_REDIS_LOCK = threading.Lock()
_REDIS_INIT_ATTEMPTED = False
_REDIS_AVAILABLE = False

DEFAULT_TTL_SECONDS = int(os.getenv("CACHE_DEFAULT_TTL_SECONDS", "60"))
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"
KEY_PREFIX = os.getenv("CACHE_KEY_PREFIX", "lms")


def _build_client() -> Optional["redis.Redis"]:
    if redis is None:
        logger.warning("redis package not installed; cache disabled")
        return None
    url = os.getenv("REDIS_URL")
    if not url:
        logger.info("REDIS_URL not set; cache disabled")
        return None
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            health_check_interval=30,
        )
        client.ping()
        logger.info("Redis cache connected")
        return client
    except Exception as exc:  # pragma: no cover - depends on infra
        logger.warning("Redis cache unavailable: %s", exc)
        return None


def get_client() -> Optional["redis.Redis"]:
    """Lazy singleton accessor for the Redis client.

    Initialization is attempted once; afterwards we cache the result.
    Subsequent operational errors keep the client but degrade individual calls.
    """
    global _REDIS_CLIENT, _REDIS_INIT_ATTEMPTED, _REDIS_AVAILABLE
    if not CACHE_ENABLED:
        return None
    if _REDIS_INIT_ATTEMPTED:
        return _REDIS_CLIENT
    with _REDIS_LOCK:
        if _REDIS_INIT_ATTEMPTED:
            return _REDIS_CLIENT
        _REDIS_CLIENT = _build_client()
        _REDIS_AVAILABLE = _REDIS_CLIENT is not None
        _REDIS_INIT_ATTEMPTED = True
        return _REDIS_CLIENT


def is_available() -> bool:
    return get_client() is not None


def _full_key(key: str) -> str:
    return f"{KEY_PREFIX}:{key}"


def get_json(key: str) -> Any:
    client = get_client()
    if client is None:
        return None
    try:
        raw = client.get(_full_key(key))
    except Exception as exc:
        logger.debug("cache get failed for %s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def set_json(key: str, value: Any, ttl_seconds: Optional[int] = None) -> bool:
    client = get_client()
    if client is None:
        return False
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS
    if ttl <= 0:
        return False
    try:
        payload = json.dumps(jsonable_encoder(value), separators=(",", ":"), default=str)
        client.set(_full_key(key), payload, ex=ttl)
        return True
    except Exception as exc:
        logger.debug("cache set failed for %s: %s", key, exc)
        return False


def delete(*keys: str) -> int:
    client = get_client()
    if client is None or not keys:
        return 0
    try:
        return int(client.delete(*[_full_key(k) for k in keys]))
    except Exception as exc:
        logger.debug("cache delete failed for %s: %s", keys, exc)
        return 0


def invalidate(*patterns: str) -> int:
    """Delete every key matching one of the given glob patterns.

    Patterns are appended to the global key prefix automatically. Uses SCAN
    in batches so we never block Redis with KEYS on production.
    """
    client = get_client()
    if client is None or not patterns:
        return 0
    removed = 0
    for pattern in patterns:
        match = _full_key(pattern)
        try:
            cursor = 0
            while True:
                cursor, keys = client.scan(cursor=cursor, match=match, count=500)
                if keys:
                    removed += int(client.delete(*keys))
                if cursor == 0:
                    break
        except Exception as exc:
            logger.debug("cache invalidate failed for %s: %s", pattern, exc)
    if removed:
        logger.debug("cache invalidated %d keys (patterns=%s)", removed, list(patterns))
    return removed


def invalidate_many(patterns: Iterable[str]) -> int:
    return invalidate(*list(patterns))


def clear_all() -> int:
    """Drop every key owned by this application's prefix."""
    return invalidate("*")


def _stable_hash(parts: Iterable[Any]) -> str:
    encoder = jsonable_encoder(list(parts), custom_encoder={})
    raw = json.dumps(encoder, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def cached(
    namespace: str,
    ttl: Optional[int] = None,
    user_arg: str = "current_user",
    key_args: Optional[Iterable[str]] = None,
):
    """Cache a FastAPI route's return value in Redis.

    Args:
        namespace: Logical key prefix (e.g. ``"progress:overview"``). Use
            colon-separated segments so ``invalidate("progress:*")`` works.
        ttl: Seconds before the entry expires. Falls back to the default.
        user_arg: Name of the FastAPI dependency argument that carries the
            current user. The user id is added to the key automatically.
        key_args: Additional handler argument names to include verbatim
            in the cache key. Use this for path/query parameters that change
            the response (e.g. ``("course_id", "limit")``).

    The handler must be ``async def``. ``db`` and other Session-like deps are
    ignored; only ``user_arg`` and ``key_args`` participate in the key.
    """
    selected = tuple(key_args or ())

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            client = get_client()
            if client is None:
                return await func(*args, **kwargs)

            try:
                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
            except TypeError:
                return await func(*args, **kwargs)

            user = bound.arguments.get(user_arg)
            user_id = getattr(user, "id", None) if user is not None else None
            user_role = getattr(user, "role", None) if user is not None else None

            key_parts = [
                str(user_id) if user_id is not None else "anon",
                str(user_role) if user_role is not None else "norole",
            ]
            for name in selected:
                key_parts.append(f"{name}={bound.arguments.get(name)!r}")
            key = f"{namespace}:{_stable_hash(key_parts)}"

            hit = get_json(key)
            if hit is not None:
                return hit

            result = await func(*args, **kwargs)
            set_json(key, result, ttl_seconds=ttl)
            return result

        wrapper.__cache_namespace__ = namespace  # type: ignore[attr-defined]
        return wrapper

    return decorator
