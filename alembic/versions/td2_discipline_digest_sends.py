"""The claim row for the daily fines post to «Штрафы учителя».

Revision ID: td2_discipline_digest
Revises: td1_teacher_discipline
"""
import sqlalchemy as sa
from alembic import op

revision = "td2_discipline_digest"
down_revision = "td1_teacher_discipline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discipline_digest_sends",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="fines"),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("kind", "day", name="uq_discipline_digest_kind_day"),
    )
    op.create_index("ix_discipline_digest_sends_day", "discipline_digest_sends", ["day"])


def downgrade() -> None:
    op.drop_index("ix_discipline_digest_sends_day", table_name="discipline_digest_sends")
    op.drop_table("discipline_digest_sends")
