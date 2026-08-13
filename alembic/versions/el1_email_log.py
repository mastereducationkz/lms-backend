"""Email delivery journal plus the counter that throttles password-reset mail.

``email_log`` records one row per recipient per send, keyed to Resend's message id so the
webhook can move it to delivered/bounced/complained. ``idempotency_key`` is unique on
purpose: claiming that key before sending is what stops the lesson-reminder scheduler
re-sending a whole cohort after a restart.

``email_rate_limit`` counts allowed forgot-password requests per email and per IP. It is a
separate table because it counts requests for addresses that do not exist — which never
produce a journal row — and because client IPs do not belong in the journal.

Revision ID: el1_email_log
Revises: ca1_crm_audit_outbox
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "el1_email_log"
down_revision: Union[str, Sequence[str], None] = "ca1_crm_audit_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "email_log" not in existing:
        op.create_table(
            "email_log",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("recipient_email", sa.String(length=320), nullable=False),
            sa.Column("recipient_user_id", sa.Integer(), nullable=True),
            sa.Column("subject", sa.String(length=500), nullable=False),
            sa.Column(
                "template_version", sa.String(length=16), server_default="v1", nullable=False
            ),
            sa.Column("related_type", sa.String(length=32), nullable=True),
            sa.Column("related_id", sa.Integer(), nullable=True),
            sa.Column("provider_message_id", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
            sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("idempotency_key", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key", name="uq_email_log_idempotency_key"),
        )
        op.create_index("ix_email_log_event_type", "email_log", ["event_type"])
        op.create_index("ix_email_log_recipient_email", "email_log", ["recipient_email"])
        op.create_index("ix_email_log_recipient_user_id", "email_log", ["recipient_user_id"])
        op.create_index(
            "ix_email_log_provider_message_id", "email_log", ["provider_message_id"]
        )
        op.create_index("ix_email_log_created_at", "email_log", ["created_at"])
        op.create_index("ix_email_log_event_created", "email_log", ["event_type", "created_at"])
        op.create_index("ix_email_log_status_created", "email_log", ["status", "created_at"])

    if "email_rate_limit" not in existing:
        op.create_table(
            "email_rate_limit",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("scope", sa.String(length=32), nullable=False),
            sa.Column("key", sa.String(length=320), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_email_rate_limit_scope_key_created",
            "email_rate_limit",
            ["scope", "key", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "email_rate_limit" in existing:
        op.drop_index("ix_email_rate_limit_scope_key_created", table_name="email_rate_limit")
        op.drop_table("email_rate_limit")

    if "email_log" in existing:
        for index in (
            "ix_email_log_status_created",
            "ix_email_log_event_created",
            "ix_email_log_created_at",
            "ix_email_log_provider_message_id",
            "ix_email_log_recipient_user_id",
            "ix_email_log_recipient_email",
            "ix_email_log_event_type",
        ):
            op.drop_index(index, table_name="email_log")
        op.drop_table("email_log")
