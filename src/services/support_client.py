"""Outbound client to the Support platform's ``/service-api``.

The other half of a handshake that already exists in one direction: Support
calls us at ``/support-api/*`` with ``X-API-Key`` (see
``src/routes/support_api.py``). This is the same protocol pointed the other way,
so the announcements UI can reach the Telegram bot -- which lives entirely on
the Support side, because it is the only process that ingests the bot's update
stream and therefore the only one that can know which groups the bot is in.

Two headers matter:

- ``X-API-Key`` proves the *service* is us.
- ``X-Acting-User`` names the *human*, and Support records it on every
  announcement. We assert it rather than forwarding a user token because the
  acting user may have no Support account at all: ``head_teacher`` -- one of the
  roles allowed to broadcast -- collapses to ``teacher`` in Support's role
  vocabulary, so this side is the only one that can tell them apart. That makes
  the role gate in ``src/announcements/routes/announcements.py`` the real
  authorization boundary for this feature.

NOTE ON NAMING: the environment variable is ``SUPPORT_SERVICE_API_KEY``, NOT
``SUPPORT_API_KEY``. The latter is already taken and means the opposite thing --
the key Support presents when calling *us*. Reusing it would silently hand our
inbound credential to an outbound caller.

Sync ``requests`` rather than async ``httpx`` to match every other outbound
integration in this repo (``src/crm_audit/drainer.py``), so these routes stay
ordinary sync FastAPI handlers running in the threadpool.
"""

import logging
import os
from typing import Any, Optional

import requests
from fastapi import HTTPException

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
# Uploads carry image bytes and Support may be re-uploading them to Telegram.
UPLOAD_TIMEOUT_SECONDS = 60


def _base() -> str:
    return (os.getenv("SUPPORT_API_BASE") or "").strip().rstrip("/")


def _key() -> str:
    return (os.getenv("SUPPORT_SERVICE_API_KEY") or "").strip()


def _headers(actor_email: str, actor_name: str) -> dict:
    return {
        "X-API-Key": _key(),
        "X-Acting-User": actor_email,
        "X-Acting-Name": actor_name or "",
    }


#: The detail of a 502 raised because Support could not be reached at all. Support itself also
#: answers 502 (a Telegram refusal that will not go away); callers that retry need to tell the two
#: apart, and comparing with this constant is how.
UNREACHABLE_DETAIL = "Could not reach the Support platform"


def call(
    method: str,
    path: str,
    *,
    actor_email: str,
    actor_name: str = "",
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    files: Optional[list] = None,
) -> Any:
    """Proxy one call to Support and return its parsed JSON.

    Support's error responses are passed through with their status and
    ``detail`` intact. That is deliberate: the whole value of this screen is
    telling staff *why* a chat missed the message ("bot was kicked from the
    group"), and a proxy that flattened everything to 502 would destroy exactly
    the information the feature exists to surface.

    An unconfigured integration raises 503 rather than degrading quietly --
    unlike the read-only context lookups, silence here would look to the user
    like a broadcast that worked.
    """
    base = _base()
    if not base or not _key():
        raise HTTPException(
            status_code=503,
            detail="Support integration is not configured (SUPPORT_API_BASE / SUPPORT_SERVICE_API_KEY)",
        )

    url = f"{base}/service-api{path}"
    try:
        response = requests.request(
            method,
            url,
            headers=_headers(actor_email, actor_name),
            json=json_body,
            params=params,
            data=data,
            files=files,
            timeout=UPLOAD_TIMEOUT_SECONDS if files else TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("support-api %s %s failed: %s", method, path, exc)
        raise HTTPException(status_code=502, detail=UNREACHABLE_DETAIL) from exc

    if response.status_code >= 400:
        detail = "Support platform rejected the request"
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = body["detail"]
        except ValueError:
            pass
        raise HTTPException(status_code=response.status_code, detail=detail)

    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="Support platform returned an unreadable response"
        ) from exc
