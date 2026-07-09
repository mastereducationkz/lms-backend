"""Client for the Phase-0 SSO handoff broker.

LMS is a *credential authority* in Phase 0: after it verifies a user's password
against its own ``users`` table, it asks the broker to sign a short-lived handoff
assertion that a relying party (CRM) redeems. LMS never signs tokens itself — the
private key lives only in the broker — so this module is a thin HTTP client.

Everything is gated by ``SSO_BROKER_ENABLED`` (off by default) so the feature is
fully dual-run: when disabled, the legacy login paths are untouched.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def sso_broker_enabled() -> bool:
    return os.getenv("SSO_BROKER_ENABLED", "").strip().lower() in _TRUTHY


def _broker_url() -> str:
    return os.getenv("SSO_BROKER_URL", "").strip().rstrip("/")


class SsoBrokerError(RuntimeError):
    """Broker unreachable, misconfigured, or returned an error."""


def mint_handoff(
    *,
    sub: str,
    aud: str,
    scope: list[str],
    email: str | None = None,
    name: str | None = None,
    tgt: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """Call the broker ``POST /handoff/mint``.

    Returns the broker's ``{handoff, jti, expires_at}``. Raises
    :class:`SsoBrokerError` on any misconfiguration or transport/HTTP failure so
    callers can surface a clean 502 without leaking internals.
    """
    url = _broker_url()
    if not url:
        raise SsoBrokerError("SSO_BROKER_URL is not configured")
    mint_key = os.getenv("SSO_BROKER_MINT_KEY", "")

    payload: dict = {"sub": sub, "aud": aud, "scope": scope, "purpose": "session_handoff"}
    if tgt is not None:
        payload["tgt"] = tgt
    if email is not None:
        payload["email"] = email
    if name is not None:
        payload["name"] = name

    try:
        resp = httpx.post(
            f"{url}/handoff/mint",
            json=payload,
            headers={"X-Broker-Service-Key": mint_key},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("SSO broker mint failed: %s", exc)
        raise SsoBrokerError(str(exc)) from exc
