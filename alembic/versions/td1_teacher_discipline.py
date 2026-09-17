"""teacher discipline register: decisions and closed periods

Revision ID: td1_teacher_discipline
Revises: exc2_attendance_excused_trigger
Create Date: 2026-09-18
"""
import sqlalchemy as sa
from alembic import op

revision = "td1_teacher_discipline"
down_revision = "exc2_attendance_excused_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discipline_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=True),
        sa.Column("teacher_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=True),
        sa.Column("proposed_amount", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("event_id", "teacher_id", "kind", name="uq_discipline_decision_lesson_kind"),
    )
    op.create_index("ix_discipline_decisions_event_id", "discipline_decisions", ["event_id"])
    op.create_index("ix_discipline_decisions_teacher_day", "discipline_decisions", ["teacher_id", "day"])

    op.create_table(
        "discipline_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("totals", sa.JSON(), nullable=True),
    )
    op.create_index("ix_discipline_periods_period_key", "discipline_periods", ["period_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_discipline_periods_period_key", table_name="discipline_periods")
    op.drop_table("discipline_periods")
    op.drop_index("ix_discipline_decisions_teacher_day", table_name="discipline_decisions")
    op.drop_index("ix_discipline_decisions_event_id", table_name="discipline_decisions")
    op.drop_table("discipline_decisions")
