"""CRM audit outbox: table plus the triggers that fill it.

The CRM audits its own mutations inline; changes made inside the LMS were invisible to it.
This adds the LMS half — a transactional outbox written by triggers, in the same transaction
as the mutation, drained to the CRM afterwards.

Revision ID: ca1_crm_audit_outbox
Revises: fz1_student_freeze_mirror
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ca1_crm_audit_outbox"
down_revision: Union[str, Sequence[str], None] = "fz1_student_freeze_mirror"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "crm_audit_outbox" not in inspector.get_table_names():
        op.create_table(
            "crm_audit_outbox",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_id", sa.String(length=64), nullable=False),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "status", sa.String(length=16), server_default="pending", nullable=False
            ),
            sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", name="uq_crm_audit_outbox_event_id"),
        )
        op.create_index(
            "ix_crm_audit_outbox_event_id", "crm_audit_outbox", ["event_id"], unique=True
        )
        op.create_index(
            "ix_crm_audit_outbox_next_attempt_at", "crm_audit_outbox", ["next_attempt_at"]
        )
        op.create_index(
            "ix_crm_audit_outbox_status_next",
            "crm_audit_outbox",
            ["status", "next_attempt_at"],
        )

    # Triggers are PostgreSQL-only; the test suite runs on SQLite, where the table alone is
    # what the model needs.
    if bind.dialect.name == "postgresql":
        from src.crm_audit.triggers import install_sql

        op.execute(sa.text(install_sql()))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from src.crm_audit.triggers import uninstall_sql

        op.execute(sa.text(uninstall_sql()))

    inspector = sa.inspect(bind)
    if "crm_audit_outbox" in inspector.get_table_names():
        op.drop_index("ix_crm_audit_outbox_status_next", table_name="crm_audit_outbox")
        op.drop_index("ix_crm_audit_outbox_next_attempt_at", table_name="crm_audit_outbox")
        op.drop_index("ix_crm_audit_outbox_event_id", table_name="crm_audit_outbox")
        op.drop_table("crm_audit_outbox")
