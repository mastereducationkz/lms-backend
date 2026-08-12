"""Transactional outbox for audit events the LMS owns.

The CRM audits its own mutations inline. Changes made *inside* the LMS — the admin group
screens, `/events` attendance, the bulk schedule upload — were invisible to it: the CRM reads
this database through a read-only session and would find a group renamed with nobody
recorded as having renamed it.

Why an outbox and not an HTTP call from the request path:

* the brief forbids "best effort after commit" as the final guarantee. A call made after the
  commit is lost if the process dies in between, and a call made *before* it records
  something that may then roll back;
* it also forbids letting an audit failure turn a committed mutation into a misleading 500.

A row written by a trigger, in the same transaction as the mutation, satisfies both: it
commits with the change or not at all, and the CRM never appears in the caller's latency or
error path. A drainer delivers the rows afterwards and may retry freely, because every row
carries a stable ``event_id`` the CRM uses as an idempotency key.

Separate from :class:`StudentSyncOutbox` on purpose. That one syncs *state* to SAT/IELTS and
its rows are safe to drop once superseded; these are an audit trail, where dropping a row
loses history. Different retention, different consumer, different table.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy import JSON

from src.models.base import Base


class CrmAuditOutbox(Base):
    """One LMS-owned audit event awaiting delivery to the CRM.

    ``event_id`` is the consumer-side idempotency key: the CRM stores it as
    ``audit_log.idempotency_key`` (namespaced ``lms:``) under a unique index, so redelivering
    a row writes nothing the second time. That is what makes at-least-once delivery safe.
    """

    __tablename__ = "crm_audit_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, unique=True, index=True)
    action = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False, default="pending", server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    published_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_crm_audit_outbox_status_next", "status", "next_attempt_at"),
    )
