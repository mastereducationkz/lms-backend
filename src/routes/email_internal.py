"""Internal email routes: Resend's delivery webhook, and the journal the CRM proxies.

The webhook is the only reason the journal can say *delivered* rather than merely *sent*.
Resend accepting a message means it entered their queue; a bounce arrives seconds to
minutes later, over this endpoint, and is the answer to "the student says nothing came".

Signature verification is written out by hand because the ``svix`` library is not a
dependency of this service and the alternative — trusting an unauthenticated public POST
that mutates delivery state — is not one. The scheme is svix's documented one: HMAC-SHA256
over ``{id}.{timestamp}.{body}`` with the base64 secret, compared in constant time, with a
five-minute clock skew window so a captured request cannot be replayed indefinitely.

With ``RESEND_WEBHOOK_SECRET`` unset the endpoint answers 503 and does nothing: an
unconfigured deployment is inert rather than open. That is the same fail-closed shape the
``/internal/crm`` routes use for their service key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.crm_internal import _require_crm_internal_key
from src.services import email_log
from src.services.email_log import EmailLog

logger = logging.getLogger(__name__)

router = APIRouter()

#: Resend event → journal status. Everything else (``email.sent``, ``email.opened``,
#: ``email.clicked``, ``email.delivery_delayed``) is acknowledged and ignored: opens are
#: pixel-based and unreliable, and "sent" is already recorded at the call site.
_EVENT_STATUS = {
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
}

_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


def _webhook_secret() -> str:
    return (os.getenv("RESEND_WEBHOOK_SECRET") or "").strip()


def verify_svix_signature(
    secret: str,
    svix_id: str,
    svix_timestamp: str,
    signature_header: str,
    body: bytes,
    *,
    now: Optional[float] = None,
) -> bool:
    """Check a svix-style webhook signature. Pure, so the rules are unit-testable.

    ``signature_header`` is a space-separated list of ``v1,<base64>`` — svix sends more
    than one during a secret rotation, and any of them matching is a pass.
    """
    if not secret or not svix_id or not svix_timestamp or not signature_header:
        return False

    try:
        sent_at = int(svix_timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - sent_at) > _TIMESTAMP_TOLERANCE_SECONDS:
        logger.warning("[EMAIL-WEBHOOK] rejected: timestamp outside tolerance")
        return False

    key_part = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        key = base64.b64decode(key_part)
    except Exception:
        return False

    signed = b"%s.%s." % (svix_id.encode(), svix_timestamp.encode()) + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()

    for candidate in signature_header.split():
        version, _, value = candidate.partition(",")
        if version != "v1" or not value:
            continue
        if hmac.compare_digest(value, expected):
            return True
    return False


@router.post("/webhook")
async def resend_webhook(
    request: Request,
    svix_id: Annotated[Optional[str], Header(alias="svix-id")] = None,
    svix_timestamp: Annotated[Optional[str], Header(alias="svix-timestamp")] = None,
    svix_signature: Annotated[Optional[str], Header(alias="svix-signature")] = None,
) -> dict:
    """Record a Resend delivery event against the journal row it belongs to."""
    secret = _webhook_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="LMS: set RESEND_WEBHOOK_SECRET to enable the Resend webhook",
        )

    body = await request.body()
    if not verify_svix_signature(
        secret, svix_id or "", svix_timestamp or "", svix_signature or "", body
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed JSON body")

    event = (payload.get("type") or "").strip()
    status = _EVENT_STATUS.get(event)
    if status is None:
        # 200 on purpose: an unhandled event type is not a delivery failure, and svix
        # retries anything non-2xx.
        return {"ok": True, "event": event, "updated": 0, "handled": False}

    data = payload.get("data") or {}
    message_id = (data.get("email_id") or data.get("id") or "").strip()
    if not message_id:
        return {"ok": True, "event": event, "updated": 0, "handled": False}

    updated = email_log.apply_provider_event(message_id, status)
    logger.info("[EMAIL-WEBHOOK] %s → %s rows for message %s", event, updated, message_id)
    return {"ok": True, "event": event, "updated": updated, "handled": True}


def _parse_utc(value: Optional[str], field: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}; expected ISO 8601")
    # Journal timestamps are naive UTC, so compare like with like.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@router.get("/log", dependencies=[Depends(_require_crm_internal_key)])
def read_email_log(
    event_type: Optional[str] = None,
    recipient: Optional[str] = None,
    status: Optional[str] = None,
    date_from: Optional[str] = Query(None, description="UTC ISO 8601, inclusive"),
    date_to: Optional[str] = Query(None, description="UTC ISO 8601, inclusive"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """The admin journal, newest first, for the CRM to proxy.

    Returns metadata only. There are no bodies to leak — the journal never stored any —
    and no field here carries a token, password or provider key.
    """
    query = db.query(EmailLog)
    if event_type:
        query = query.filter(EmailLog.event_type == event_type)
    if status:
        query = query.filter(EmailLog.status == status)
    if recipient:
        query = query.filter(EmailLog.recipient_email.ilike(f"%{recipient.strip()}%"))
    start = _parse_utc(date_from, "date_from")
    if start is not None:
        query = query.filter(EmailLog.created_at >= start)
    end = _parse_utc(date_to, "date_to")
    if end is not None:
        query = query.filter(EmailLog.created_at <= end)

    total = query.count()
    rows = (
        query.order_by(EmailLog.created_at.desc(), EmailLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": [
            {
                "id": row.id,
                "event_type": row.event_type,
                "recipient_email": row.recipient_email,
                "recipient_user_id": row.recipient_user_id,
                "subject": row.subject,
                "template_version": row.template_version,
                "related_type": row.related_type,
                "related_id": row.related_id,
                "provider_message_id": row.provider_message_id,
                "status": row.status,
                "attempts": row.attempts,
                "error": row.error,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/log/event-types", dependencies=[Depends(_require_crm_internal_key)])
def read_event_types() -> dict:
    """The filter vocabulary, so the CRM dropdown cannot drift from the backend."""
    return {"event_types": list(email_log.EVENT_TYPES), "statuses": list(email_log.STATUSES)}


@router.get("/log/{log_id}", dependencies=[Depends(_require_crm_internal_key)])
def read_log_entry(log_id: int, db: Session = Depends(get_db)) -> dict:
    """One journal row in full, including the body when there is one to show.

    **Reading is not sending.** This is a plain SELECT: opening a row must never re-deliver
    the mail, and there is no code path from here into the send. The at-most-once guarantee
    that ``idempotency_key`` buys is unaffected because nothing here writes.

    Three distinct answers about content, which the caller must be able to tell apart:

    * ``content_withheld`` — deliberately not stored: the mail carries a password or a reset
      link. The notice says so.
    * ``has_content = false`` with ``content_withheld = false`` — this row predates content
      capture. Nothing is hidden and nothing is invented; the view says it is unavailable.
    * a body — stored, and already stripped of script/form/remote-resource content on the way
      in. The CRM still renders it inside a sandboxed iframe; that is the second layer.
    """
    row = db.query(email_log.EmailLog).filter(email_log.EmailLog.id == log_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Письмо не найдено")

    withheld = bool(row.content_withheld)
    return {
        "id": row.id,
        "event_type": row.event_type,
        "recipient_email": row.recipient_email,
        "recipient_user_id": row.recipient_user_id,
        "subject": row.subject,
        "template_version": row.template_version,
        "related_type": row.related_type,
        "related_id": row.related_id,
        "provider_message_id": row.provider_message_id,
        "status": row.status,
        "attempts": row.attempts,
        # Already sanitized when written — `sanitize_error` drops the provider's echoed
        # payload entirely for credential mail.
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "content_withheld": withheld,
        "content_notice": email_log.CONTENT_WITHHELD_NOTICE if withheld else None,
        # Never fall back to the other field when withheld: a credential body must be absent
        # from the response, not merely unrendered by the client.
        "body_html": None if withheld else row.body_html,
        "body_text": None if withheld else row.body_text,
        "has_content": bool(not withheld and (row.body_html or row.body_text)),
    }
