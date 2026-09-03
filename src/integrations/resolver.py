"""Map a platform's student reference to an LMS user (Platform Integration Pack §1).

Order everywhere: Zitadel subject (``users.central_auth_user_id``) first, lowercased email
second. Email stays the fallback forever — nothing that works today by email stops working.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session


def resolve_user_id(db: Session, *, zitadel_subject: Optional[str], email: Optional[str]) -> Optional[int]:
    from src.auth.models import UserInDB

    if zitadel_subject:
        user_id = (
            db.query(UserInDB.id)
            .filter(UserInDB.central_auth_user_id == zitadel_subject)
            .order_by(UserInDB.id.asc())
            .limit(1)
            .scalar()
        )
        if user_id is not None:
            return user_id
    email = (email or "").strip().lower()
    if email:
        return (
            db.query(UserInDB.id)
            .filter(func.lower(func.trim(UserInDB.email)) == email)
            .order_by(UserInDB.id.asc())
            .limit(1)
            .scalar()
        )
    return None
