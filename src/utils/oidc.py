"""OIDC relying-party token verification (SSO Phase 2, dual-run scaffolding).

Validates RS256 access tokens issued by the central IdP (Zitadel) against its
published JWKS, so LMS can accept IdP-issued tokens *alongside* its legacy HS256
tokens during the migration. This module is the reusable, standards-shaped
verifier; it is deliberately **not wired into the auth path yet** — the dual-run
acceptance behind the ``OIDC_ACCEPT`` flag will call ``verify_oidc_token`` once
the IdP is live and the wiring is approved.

Design notes:
- ``verify_oidc_claims`` is the pure security core: given a JWKS dict it performs
  full RS256 validation (signature, issuer allow-list, audience, exp/iat/sub).
  It is unit-testable with a locally generated key — no network, no running IdP.
- ``verify_oidc_token`` reads config, fetches+caches the JWKS over HTTPS, and
  delegates to the core. Only this outer function touches the network.
- Algorithm discipline: RS256 only. HS256 / ``alg:none`` are rejected, closing the
  classic algorithm-confusion hole that a shared-secret estate is prone to.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


class OidcConfigError(RuntimeError):
    """OIDC acceptance is enabled but misconfigured (missing issuer/JWKS/audience)."""


class OidcVerifyError(Exception):
    """The presented token is not a valid, trusted OIDC token."""


# --- configuration (all env-driven; every default keeps the feature OFF) --------

def oidc_accept_enabled() -> bool:
    """True when LMS should accept IdP RS256 tokens in addition to legacy HS256."""
    return os.getenv("OIDC_ACCEPT", "").strip().lower() in _TRUTHY


def oidc_issuers() -> list[str]:
    """Trusted issuer allow-list (comma-separated ``OIDC_ISSUERS``)."""
    raw = os.getenv("OIDC_ISSUERS", "")
    return [i.strip().rstrip("/") for i in raw.split(",") if i.strip()]


def oidc_jwks_url() -> str:
    return os.getenv("OIDC_JWKS_URL", "").strip()


def oidc_audiences() -> list[str]:
    """Audiences this resource server (LMS) accepts (comma-separated ``OIDC_AUDIENCE``).

    Zitadel puts resource IDs (project app IDs) in a token's ``aud``, not a free-form
    string, so this is normally the ``lms-api`` resource ID (and, tolerantly, the web
    /mobile client IDs). A token is accepted if its ``aud`` contains ANY of these.
    """
    raw = os.getenv("OIDC_AUDIENCE", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def oidc_leeway_seconds() -> int:
    try:
        return int(os.getenv("OIDC_LEEWAY_SECONDS", "60"))
    except ValueError:
        return 60


def oidc_userinfo_url() -> str:
    """OIDC userinfo endpoint, used to resolve `email` when it is absent from the token
    (Zitadel JWT access tokens don't carry email; it comes from userinfo or the ID token)."""
    return os.getenv("OIDC_USERINFO_URL", "").strip()


def fetch_userinfo(token: str, *, timeout: float = 5.0) -> dict:
    """Call the userinfo endpoint with the presented access token. Returns {} on any
    failure or if no URL is configured (caller treats a missing email as auth failure)."""
    url = oidc_userinfo_url()
    if not url:
        return {}
    try:
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except (httpx.HTTPError, ValueError) as exc:  # transport or non-JSON
        logger.info("OIDC userinfo fetch failed: %s", exc)
        return {}


# --- JWKS fetch + cache ---------------------------------------------------------

_jwks_cache: dict[str, tuple[float, dict]] = {}
_jwks_lock = threading.Lock()
_JWKS_TTL_SECONDS = 600  # 10 minutes


def _fetch_jwks(url: str, *, timeout: float = 5.0) -> dict:
    """Fetch a JWKS document with a short TTL cache. Raises OidcConfigError on failure."""
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache.get(url)
        if cached and (now - cached[0]) < _JWKS_TTL_SECONDS:
            return cached[1]
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        jwks = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcConfigError(f"could not fetch JWKS from {url}: {exc}") from exc
    if not isinstance(jwks, dict) or "keys" not in jwks:
        raise OidcConfigError(f"JWKS at {url} is malformed")
    with _jwks_lock:
        _jwks_cache[url] = (now, jwks)
    return jwks


def clear_jwks_cache() -> None:
    with _jwks_lock:
        _jwks_cache.clear()


# --- verification ---------------------------------------------------------------

def verify_oidc_claims(
    token: str,
    *,
    jwks: dict,
    issuers: list[str],
    audience,  # str | list[str] — PyJWT accepts either; a list passes if ANY matches
    leeway: int = 60,
) -> dict:
    """Full RS256 validation against a JWKS dict. Returns the verified claims.

    Raises :class:`OidcVerifyError` on any failure. Pure (no network), so it is the
    unit-testable security core.
    """
    if not issuers:
        raise OidcVerifyError("no trusted issuers configured")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OidcVerifyError(f"malformed token: {exc}") from exc

    if header.get("alg") != "RS256":
        # Reject HS256 / none up front — never let a non-RS256 token reach decode.
        raise OidcVerifyError(f"unexpected alg {header.get('alg')!r}; RS256 required")

    kid = header.get("kid")
    signing_key = None
    for entry in jwks.get("keys", []):
        if entry.get("kid") == kid and entry.get("kty") == "RSA":
            try:
                signing_key = jwt.PyJWK.from_dict(entry).key
            except jwt.PyJWTError as exc:
                raise OidcVerifyError(f"invalid JWKS key: {exc}") from exc
            break
    if signing_key is None:
        raise OidcVerifyError("no matching signing key (kid) in JWKS")

    # Issuer must be in the allow-list. PyJWT's issuer= takes a single value, so we
    # read the unverified iss, check membership, then verify against it.
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise OidcVerifyError(f"undecodable token: {exc}") from exc
    iss = unverified.get("iss")
    if iss not in issuers:
        raise OidcVerifyError(f"untrusted issuer {iss!r}")

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=iss,
            leeway=leeway,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OidcVerifyError(str(exc)) from exc
    return claims


def verify_oidc_token(token: str) -> dict:
    """Verify an IdP token using the configured issuer allow-list + fetched JWKS.

    Raises :class:`OidcConfigError` if OIDC config is incomplete, or
    :class:`OidcVerifyError` if the token is not valid/trusted.
    """
    issuers = oidc_issuers()
    jwks_url = oidc_jwks_url()
    audiences = oidc_audiences()
    if not issuers or not jwks_url or not audiences:
        raise OidcConfigError("OIDC_ISSUERS, OIDC_JWKS_URL and OIDC_AUDIENCE must be set to accept OIDC tokens")
    jwks = _fetch_jwks(jwks_url)
    return verify_oidc_claims(
        token,
        jwks=jwks,
        issuers=issuers,
        audience=audiences,
        leeway=oidc_leeway_seconds(),
    )
