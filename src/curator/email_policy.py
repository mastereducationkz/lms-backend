"""Curators are told things in the application, never by email — enforced again, here.

The CRM already clears the email flag before calling this service. This module is the
independent second check, and it exists because the first one lives in a different
repository, on the other side of an HTTP boundary, and can be redeployed without this one.
A policy enforced only by the caller is enforced by nobody: an old CRM build, a replayed
request, a future direct caller of ``/internal/crm/curator/notify``, or a call site inside
this repository that composes curator mail on its own would all sail straight past it.

The rule:

* holding ``curator`` or ``head_curator`` is enough — a person who is also something else is
  still a curator, and the operational mail they would get is curator mail;
* there is no "mandatory" tier that overrides it;
* the in-app notification is unaffected. Only the email is withheld.

Authentication and account-recovery mail — invitations, password reset, password changed —
is **not** operational mail and does not pass through here. It keeps working for everybody.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Roles that receive operational notifications in-app only.
CURATOR_ROLES = frozenset({"curator", "head_curator"})

#: Recorded in the email journal so a withheld message is distinguishable from a lost one.
SUPPRESSION_REASON = "curator_in_app_only"

#: The only mail a curator may still receive: identity and account recovery.
#:
#: Expressed as an allow-list of *authentication* types rather than a block-list of
#: operational ones, and that direction is deliberate. A block-list has to be extended every
#: time somebody adds an event type, and the failure mode of forgetting is a leaked email. An
#: allow-list's failure mode is a withheld one, which is the safe direction — and there will
#: never be a new kind of password reset.
AUTH_EVENT_TYPES = frozenset({
    "invite",
    "trial_invite",
    "password_reset",
    "password_changed",
})


def is_operational_event(event_type: Optional[str]) -> bool:
    """True for anything that is not identity/account-recovery mail."""
    return (event_type or "other").strip() not in AUTH_EVENT_TYPES


def curator_user_ids(db: Session, user_ids: Iterable[int]) -> set[int]:
    """Which of these LMS users hold a curator or head-curator role."""
    from src.schemas.models import UserInDB

    ids = sorted({int(i) for i in user_ids if i})
    if not ids:
        return set()
    return {
        int(row[0])
        for row in db.query(UserInDB.id)
        .filter(
            UserInDB.id.in_(ids),
            UserInDB.role.in_(sorted(CURATOR_ROLES)),
        )
        .all()
    }


def is_curator(db: Session, user_id: Optional[int]) -> bool:
    """True when this LMS user must not receive operational email."""
    if not user_id:
        return False
    return int(user_id) in curator_user_ids(db, [int(user_id)])


def is_curator_email(db: Session, email: Optional[str]) -> bool:
    """Same question, answered from an address.

    Needed because :func:`~src.curator.notifications.send_curator_email` is handed an address
    rather than a user id, and it is the last place a curator email could be composed.
    """
    if not email or "@" not in email:
        return False
    from src.schemas.models import UserInDB

    row = (
        db.query(UserInDB.role)
        .filter(UserInDB.email == email.strip())
        .first()
    )
    return bool(row and (row[0] or "").strip().lower() in CURATOR_ROLES)
