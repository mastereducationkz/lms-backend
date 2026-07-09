"""Resolve the LMS user for a verified token payload (SSO Phase 2).

For OIDC tokens (payload carries ``oidc`` + ``central_auth_user_id``) this prefers the
stable IdP subject, so a login keeps working even if the user's email later changes;
on the first match (by email) it backfills the id so subsequent logins use the stable
path. Legacy HS256 tokens carry neither field and resolve by email exactly as before —
byte-for-byte the same behavior, so this is a safe drop-in for the auth resolvers.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.schemas.models import UserInDB

logger = logging.getLogger(__name__)


def resolve_user_by_payload(db: Session, payload: dict) -> Optional[UserInDB]:
    email = (payload.get("sub") or "").strip()
    caid = payload.get("central_auth_user_id") if payload.get("oidc") else None

    # 1) OIDC: match on the stable IdP subject first (survives email changes).
    if caid:
        user = db.query(UserInDB).filter(UserInDB.central_auth_user_id == caid).first()
        if user is not None:
            return user

    # 2) Email match — the legacy path, and the OIDC first-login path.
    if not email:
        return None
    user = db.query(UserInDB).filter(func.lower(UserInDB.email) == email.lower()).first()

    # 3) Backfill the stable id on first OIDC match so future logins take path (1).
    #    A backfill failure must never break authentication.
    if user is not None and caid and not user.central_auth_user_id:
        user.central_auth_user_id = caid
        try:
            db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("central_auth_user_id backfill failed for user %s: %s", user.id, exc)
            db.rollback()
    return user
