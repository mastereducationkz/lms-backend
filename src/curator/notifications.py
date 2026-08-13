"""Curator workspace notifications: in-app rows + best-effort email.

Two rules shape this module.

**A notification must never undo the thing it announces.** A failed transfer email is an
annoyance; a rolled-back transfer is a data-integrity incident. So the in-app row is written
in the caller's transaction (it is cheap and local) while email is dispatched *after* the
commit, on a background task, and every failure is swallowed and logged.

**Duplicates are worse than silence.** Curators get a card per student; an overdue digest
that fires hourly would be noise within a day. :func:`notify_once` keys off the existing
``notifications`` rows so re-running a sweep does not re-notify.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from src.messages.models import Notification
from src.schemas.models import UserInDB

logger = logging.getLogger(__name__)

# Notification types (also the in-app filter keys).
TYPE_ASSIGNMENT = "curator_assignment"
TYPE_TRANSFER_IN = "curator_transfer_in"
TYPE_RESPONSIBILITY_ENDED = "curator_responsibility_ended"
TYPE_BULK_REASSIGN = "curator_bulk_reassign"
TYPE_TRANSFER_FAILED = "curator_transfer_failed"
TYPE_UNASSIGNED_GROUP = "curator_unassigned_group"
TYPE_ONBOARDING_OVERDUE = "curator_onboarding_overdue"

#: Types a head curator should hear about. Routine movement is deliberately absent —
#: the head is an exception handler, not a CC list.
HEAD_TYPES = (TYPE_TRANSFER_FAILED, TYPE_UNASSIGNED_GROUP, TYPE_ONBOARDING_OVERDUE)


def _crm_base_url() -> str:
    import os

    return (os.getenv("CRM_WEB_URL", "https://crm.mastereducation.kz") or "").rstrip("/")


def crm_onboarding_url(onboarding_id: Optional[int] = None) -> str:
    base = f"{_crm_base_url()}/curator/onboarding"
    return f"{base}?card={onboarding_id}" if onboarding_id else base


def crm_student_url(student_account_id: Optional[int], lms_student_id: Optional[int]) -> str:
    """Deep link to the CRM student card, falling back to the by-LMS-id resolver.

    The fallback matters: a student with no CRM data record still needs a working link, and
    the CRM route resolves or reconciles it there rather than leaving a dead end here.
    """
    if student_account_id:
        return f"{_crm_base_url()}/students/{student_account_id}"
    if lms_student_id:
        return f"{_crm_base_url()}/students/lms/{lms_student_id}"
    return f"{_crm_base_url()}/curator/clients"


def create_notification(
    db: Session,
    user_id: int,
    title: str,
    content: str,
    notification_type: str,
    related_id: Optional[int] = None,
) -> Notification:
    """Add an in-app notification to the caller's transaction. Caller commits."""
    row = Notification(
        user_id=user_id,
        title=title,
        content=content,
        notification_type=notification_type,
        related_id=related_id,
        is_read=False,
    )
    db.add(row)
    return row


def notify_once(
    db: Session,
    user_id: int,
    title: str,
    content: str,
    notification_type: str,
    related_id: Optional[int] = None,
    within_hours: int = 24,
) -> Optional[Notification]:
    """Create a notification unless an identical one already exists in the window.

    Identity is (user, type, related_id) — the same overdue card notified to the same person
    is the same event no matter how the sentence is phrased.
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=within_hours)
    exists = (
        db.query(Notification.id)
        .filter(
            Notification.user_id == user_id,
            Notification.notification_type == notification_type,
            Notification.related_id == related_id,
            Notification.created_at >= since,
        )
        .first()
    )
    if exists:
        return None
    return create_notification(db, user_id, title, content, notification_type, related_id)


# --- email --------------------------------------------------------------------------------


def _send_email_safe(
    to_email: str, subject: str, html: str, event_type: str = "curator_notify"
) -> bool:
    """Send one email; never raise. Returns whether Resend accepted it.

    Deliberately logs the recipient and outcome but never the body, which can carry a
    student's contact details.
    """
    try:
        from src.services.email_service import _email_shell, get_email_service

        service = get_email_service()
        # An unconfigured service is send_email's call, not ours: it records the skip in
        # the email journal and returns None, which is the same False we would return.
        result = service.send_email(
            [to_email], subject, _email_shell(subject, html), event_type=event_type
        )
        ok = result is not None
        logger.info("[CURATOR-NOTIFY] email to=%s subject=%r delivered=%s", to_email, subject, ok)
        return ok
    except Exception:
        logger.exception("[CURATOR-NOTIFY] email send failed to=%s", to_email)
        return False


def _para(text: str) -> str:
    return f'<p style="margin:0 0 12px;font-size:14px;color:#333333;">{text}</p>'


def _link_button(href: str, label: str) -> str:
    try:
        from src.services.email_service import _button

        return _button(href, label)
    except Exception:  # pragma: no cover - styling helper only
        return f'<a href="{href}">{label}</a>'


def send_curator_email(
    to_email: Optional[str],
    subject: str,
    lines: Sequence[str],
    link: Optional[str] = None,
    link_label: str = "Открыть в CRM",
    event_type: str = "curator_notify",
) -> bool:
    """Compose and send a curator notification email.

    Body carries names and links only — never balances, payments or credentials, which have
    no business leaving the CRM's authorization boundary in an inbox.
    """
    if not to_email or "@" not in to_email:
        return False
    html = "".join(_para(line) for line in lines)
    if link:
        html += f'<div style="margin-top:20px;">{_link_button(link, link_label)}</div>'
    return _send_email_safe(to_email, subject, html, event_type)


# --- high-level events --------------------------------------------------------------------


def _display_name(user: Optional[UserInDB]) -> str:
    if user is None:
        return "—"
    return (getattr(user, "official_full_name", None) or getattr(user, "name", "") or "").strip() or "—"


def notify_student_assigned(
    db: Session,
    curator: UserInDB,
    student: UserInDB,
    group_name: Optional[str],
    onboarding_id: Optional[int],
    reason: str = "assigned",
) -> list[tuple[str, str, list[str], str]]:
    """In-app now, email payload returned for the caller to dispatch post-commit.

    Returning the email instead of sending it here is what keeps the mail out of the
    caller's transaction — see the module docstring.
    """
    student_name = _display_name(student)
    where = f" в группу «{group_name}»" if group_name else ""
    title = "Новый студент на онбординге" if reason == "assigned" else "Перевод студента к вам"
    content = f"{student_name} добавлен(а){where}. Начните онбординг в CRM."
    create_notification(
        db,
        curator.id,
        title,
        content,
        TYPE_ASSIGNMENT if reason == "assigned" else TYPE_TRANSFER_IN,
        related_id=onboarding_id,
    )
    return [
        (
            (curator.email or ""),
            title,
            [f"<b>{student_name}</b> добавлен(а){where}.", "Карточка онбординга уже создана."],
            crm_onboarding_url(onboarding_id),
        )
    ]


def notify_responsibility_ended(
    db: Session,
    curator: UserInDB,
    student: UserInDB,
    group_name: Optional[str],
) -> list[tuple[str, str, list[str], str]]:
    student_name = _display_name(student)
    where = f" из группы «{group_name}»" if group_name else ""
    title = "Студент больше не за вами"
    content = f"{student_name} переведён(а){where}. Онбординг закрыт."
    create_notification(db, curator.id, title, content, TYPE_RESPONSIBILITY_ENDED)
    return [
        (
            (curator.email or ""),
            title,
            [f"<b>{student_name}</b> переведён(а){where}.", "Ваш цикл онбординга закрыт."],
            crm_onboarding_url(),
        )
    ]


def notify_heads(
    db: Session,
    heads: Iterable[UserInDB],
    title: str,
    content: str,
    notification_type: str,
    related_id: Optional[int] = None,
    link: Optional[str] = None,
) -> list[tuple[str, str, list[str], str]]:
    """Escalate to every head curator. Deduplicated per (head, type, related_id)."""
    emails: list[tuple[str, str, list[str], str]] = []
    for head in heads:
        if notify_once(db, head.id, title, content, notification_type, related_id) is not None:
            emails.append(((head.email or ""), title, [content], link or crm_onboarding_url()))
    return emails


def head_curators(db: Session) -> list[UserInDB]:
    return (
        db.query(UserInDB)
        .filter(UserInDB.role == "head_curator", UserInDB.is_active == True)  # noqa: E712
        .all()
    )


def dispatch_emails(
    payloads: Iterable[tuple[str, str, list[str], str]],
    event_type: str = "curator_notify",
) -> int:
    """Send a batch of prepared emails. Never raises; returns the delivered count.

    ``event_type`` is per batch rather than per payload because each caller dispatches one
    kind of event, and threading it through the payload tuple would change a shape four
    call sites already build.
    """
    delivered = 0
    for to_email, subject, lines, link in payloads:
        if send_curator_email(to_email, subject, lines, link, event_type=event_type):
            delivered += 1
    return delivered
