"""The email delivery journal — what the LMS sent, to whom, and what became of it.

Until now every email was fire-and-forget: :mod:`src.services.email_service` swallowed
every failure and returned ``None``, and the provider message id Resend hands back was
discarded by all 24 call sites. When a student said "I never got my password", nobody
could answer whether the LMS tried, whether Resend accepted it, or whether it bounced.

This module is the answer to that question. Every send writes one :class:`EmailLog` row
**per recipient**, and Resend's webhook later moves that row to ``delivered`` /
``bounced`` / ``complained``. Two properties matter more than completeness:

* **The journal never breaks sending.** Every write here runs inside its own short-lived
  session and its own ``try``; if the journal is down, the email still goes out and the
  failure lands on stdout. A logging table that can take down password reset would be a
  worse bug than the one it fixes.
* **The journal holds no secrets.** There is deliberately no body column. Credential
  emails (invites, admin-set passwords) carry a plaintext password in their HTML, so for
  those event types even the provider's error response is dropped rather than stored —
  Resend echoes the payload on some 4xx replies, and that payload contains the password.

``idempotency_key`` is the other half of the design. Writing the claim row *before*
sending, under a unique constraint, is what lets the lesson-reminder scheduler survive a
restart: a second attempt with the same key loses the insert and skips the send. It buys
at-most-once, not at-least-once — a process that dies between claim and send leaves a
``queued`` row and no email. For a reminder that is the right trade (a reminder delivered
forty minutes late is worse than none), and the stuck row is visible in the journal.

:class:`EmailRateLimit` shares this module because it exists only to throttle email:
``/auth/forgot-password`` had no limit at all, so anyone could use it to mail-bomb a
known address. It is kept out of :class:`EmailLog` because it counts *requests* — including
those for addresses that do not exist, which never produce a journal row — and because
client IPs do not belong in a table that ops reads day to day.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from src.models.base import Base

logger = logging.getLogger(__name__)


# --- event vocabulary -------------------------------------------------------------------

#: Every distinct kind of mail the LMS sends. Kept short and slug-like so the admin filter
#: is a dropdown, not a free-text search over subjects (which are bilingual and templated).
EVENT_TYPES = (
    "trial_invite",
    "password_reset",
    "password_changed",
    "invite",
    "homework_new",
    "homework_updated",
    "submission_graded",
    "lesson_change",
    "curator_notify",
    "curator_transfer",
    "unassigned_groups",
    "overdue_sweep",
    "curator_removed",
    "lesson_reminder",
    "other",
)

#: Event types whose rendered body contains a plaintext password. For these the provider's
#: error text is never stored — only a status code — because Resend echoes the submitted
#: payload on some failures and that payload is the credential itself.
CREDENTIAL_EVENT_TYPES = frozenset({"invite", "trial_invite", "password_changed"})

#: ``queued`` is claimed-but-not-yet-answered; ``sent`` means Resend accepted it; the last
#: three arrive later over the webhook. ``suppressed`` is the LMS declining to send at all
#: (no API key, unusable address) — recorded because "we never tried" is itself the answer
#: to most delivery questions.
STATUSES = ("queued", "sent", "failed", "delivered", "bounced", "complained", "suppressed")

TEMPLATE_VERSION = "v1"


class EmailLog(Base):
    """One attempted delivery to one recipient."""

    __tablename__ = "email_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(32), nullable=False, index=True)
    recipient_email = Column(String(320), nullable=False, index=True)
    recipient_user_id = Column(Integer, nullable=True, index=True)
    subject = Column(String(500), nullable=False)
    template_version = Column(String(16), nullable=False, default=TEMPLATE_VERSION,
                              server_default=TEMPLATE_VERSION)
    related_type = Column(String(32), nullable=True)
    related_id = Column(Integer, nullable=True)
    provider_message_id = Column(String(128), nullable=True, index=True)
    status = Column(String(16), nullable=False, default="queued", server_default="queued")
    attempts = Column(Integer, nullable=False, default=1, server_default="1")
    error = Column(Text, nullable=True)
    idempotency_key = Column(String(200), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    sent_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True, onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        # The admin journal is always "newest first, optionally narrowed by type".
        Index("ix_email_log_created_at", "created_at"),
        Index("ix_email_log_event_created", "event_type", "created_at"),
        Index("ix_email_log_status_created", "status", "created_at"),
    )


class EmailRateLimit(Base):
    """One *allowed* throttled request. Rows older than the window are dead weight.

    Only allowed requests are recorded, which bounds writes to the limit itself: a client
    hammering the endpoint cannot grow this table past its own hourly cap.
    """

    __tablename__ = "email_rate_limit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scope = Column(String(32), nullable=False)
    key = Column(String(320), nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_email_rate_limit_scope_key_created", "scope", "key", "created_at"),
    )


# --- session plumbing -------------------------------------------------------------------

_session_factory: Optional[Callable[[], Session]] = None


def set_session_factory(factory: Optional[Callable[[], Session]]) -> None:
    """Point the journal at a different session source. For tests only."""
    global _session_factory
    _session_factory = factory


def _new_session() -> Session:
    if _session_factory is not None:
        return _session_factory()
    # Imported late: src.config pulls in the whole model graph, and this module is itself
    # part of it (registered in src.models).
    from src.config import SessionLocal

    return SessionLocal()


def _log_failure(what: str, exc: BaseException) -> None:
    """A journal failure is reported and dropped — it must never reach the caller."""
    logger.error("[EMAIL-LOG] %s failed: %s: %s", what, type(exc).__name__, exc)


# --- error sanitising -------------------------------------------------------------------

_REDACTIONS = (
    (re.compile(r"re_[A-Za-z0-9_\-]{8,}"), "re_***"),
    (re.compile(r"whsec_[A-Za-z0-9+/=_\-]{8,}"), "whsec_***"),
    (re.compile(r"(?i)bearer\s+\S+"), "Bearer ***"),
    (re.compile(r"(?i)(api[-_]?key\"?\s*[:=]\s*\"?)([^\"\s,}]+)"), r"\1***"),
)

_MAX_ERROR_LEN = 500


def sanitize_error(error: object, *, event_type: str = "other") -> Optional[str]:
    """Reduce a provider failure to something safe to store.

    Credential emails get the exception *class* and nothing else: their payload is a
    password, and providers quote the payload back in error bodies.
    """
    if error is None:
        return None
    if event_type in CREDENTIAL_EVENT_TYPES:
        if isinstance(error, BaseException):
            return type(error).__name__
        # A caller-supplied string for a credential email is still not worth the risk.
        return "send_failed"
    text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > _MAX_ERROR_LEN:
        text = text[:_MAX_ERROR_LEN] + "…"
    return text


# --- writing the journal ----------------------------------------------------------------

def claim(
    *,
    event_type: str,
    recipient_email: str,
    subject: str,
    recipient_user_id: Optional[int] = None,
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
    idempotency_key: Optional[str] = None,
) -> Optional[int]:
    """Reserve a row for a send that is about to happen.

    Returns the row id, or ``None`` when the send should not proceed — either because
    ``idempotency_key`` is already taken (someone else has this one) or because the journal
    is unavailable. Those two are distinguished by :func:`claimed_elsewhere`.
    """
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover - only when the DB config itself is broken
        _log_failure("session", exc)
        return None
    try:
        row = EmailLog(
            event_type=event_type if event_type in EVENT_TYPES else "other",
            recipient_email=(recipient_email or "")[:320],
            recipient_user_id=recipient_user_id,
            subject=(subject or "")[:500],
            template_version=TEMPLATE_VERSION,
            related_type=related_type,
            related_id=related_id,
            status="queued",
            attempts=1,
            idempotency_key=idempotency_key,
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        return row.id
    except IntegrityError:
        db.rollback()
        logger.info("[EMAIL-LOG] idempotency key already claimed: %s", idempotency_key)
        return None
    except SQLAlchemyError as exc:
        db.rollback()
        _log_failure("claim", exc)
        return None
    except Exception as exc:
        db.rollback()
        _log_failure("claim", exc)
        return None
    finally:
        db.close()


def claimed_elsewhere(idempotency_key: str) -> bool:
    """True when a row already holds this key — i.e. the send has been done or is running."""
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover
        _log_failure("session", exc)
        return False
    try:
        return db.query(EmailLog.id).filter(
            EmailLog.idempotency_key == idempotency_key
        ).first() is not None
    except Exception as exc:
        _log_failure("claimed_elsewhere", exc)
        return False
    finally:
        db.close()


def finish(
    row_id: Optional[int],
    *,
    status: str,
    provider_message_id: Optional[str] = None,
    error: object = None,
    event_type: str = "other",
) -> None:
    """Close out a claimed row with the provider's answer."""
    if row_id is None:
        return
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover
        _log_failure("session", exc)
        return
    try:
        row = db.query(EmailLog).filter(EmailLog.id == row_id).first()
        if row is None:
            return
        row.status = status if status in STATUSES else "failed"
        row.provider_message_id = (provider_message_id or None)
        row.error = sanitize_error(error, event_type=event_type)
        row.updated_at = datetime.now(timezone.utc)
        if status == "sent":
            row.sent_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        _log_failure("finish", exc)
    finally:
        db.close()


def apply_provider_event(provider_message_id: str, status: str) -> int:
    """Move journal rows to a webhook-reported terminal state. Returns rows updated.

    Matching is by provider message id, the only identifier Resend and the LMS share.
    """
    if not provider_message_id or status not in STATUSES:
        return 0
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover
        _log_failure("session", exc)
        return 0
    try:
        rows = db.query(EmailLog).filter(
            EmailLog.provider_message_id == provider_message_id
        ).all()
        for row in rows:
            row.status = status
            row.updated_at = datetime.now(timezone.utc)
        db.commit()
        return len(rows)
    except Exception as exc:
        db.rollback()
        _log_failure("apply_provider_event", exc)
        return 0
    finally:
        db.close()


# --- throttling -------------------------------------------------------------------------

PASSWORD_RESET_PER_EMAIL_PER_HOUR = 3
PASSWORD_RESET_PER_IP_PER_HOUR = 10
_THROTTLE_WINDOW = timedelta(hours=1)


def password_reset_allowed(email: str, ip: Optional[str]) -> bool:
    """May this forgot-password request proceed? Records the attempt when it may.

    Counting happens in the database, not in process memory, because production runs four
    uvicorn workers plus a scheduler — an in-process counter would give an attacker four
    times the budget and reset on every deploy.

    Fails **open**: if the counter table cannot be read the request still goes through. A
    reset flow that locks everyone out whenever this table misbehaves would trade a rate
    limit for an outage, and the database is already required to issue the token at all.
    """
    normalized = (email or "").strip().lower()
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover
        _log_failure("session", exc)
        return True
    try:
        since = datetime.now(timezone.utc) - _THROTTLE_WINDOW
        checks = [("password_reset_email", normalized, PASSWORD_RESET_PER_EMAIL_PER_HOUR)]
        if ip:
            checks.append(("password_reset_ip", ip, PASSWORD_RESET_PER_IP_PER_HOUR))
        for scope, key, limit in checks:
            used = db.query(EmailRateLimit.id).filter(
                EmailRateLimit.scope == scope,
                EmailRateLimit.key == key,
                EmailRateLimit.created_at >= since,
            ).count()
            if used >= limit:
                logger.warning("[EMAIL-THROTTLE] %s over limit (%s/%s)", scope, used, limit)
                return False
        now = datetime.now(timezone.utc)
        for scope, key, _limit in checks:
            db.add(EmailRateLimit(scope=scope, key=key[:320], created_at=now))
        db.commit()
        return True
    except Exception as exc:
        try:
            db.rollback()
        except Exception:  # pragma: no cover
            pass
        _log_failure("password_reset_allowed", exc)
        return True
    finally:
        db.close()


def prune_rate_limits(older_than: timedelta = timedelta(days=1)) -> int:
    """Drop throttle rows that can no longer affect a decision."""
    try:
        db = _new_session()
    except Exception as exc:  # pragma: no cover
        _log_failure("session", exc)
        return 0
    try:
        cutoff = datetime.now(timezone.utc) - older_than
        deleted = db.query(EmailRateLimit).filter(
            EmailRateLimit.created_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
        return int(deleted or 0)
    except Exception as exc:
        db.rollback()
        _log_failure("prune_rate_limits", exc)
        return 0
    finally:
        db.close()
