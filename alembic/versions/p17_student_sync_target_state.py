"""Per-target delivery state on student_sync_outbox (Platform Integration Pack §4).

The drainer used to re-post a pending row to EVERY target on each retry, so a target that
answered 503 (SAT with its consumer flag off) made the drainer re-hit the healthy target
(IELTS) on every pass — ~63k redundant posts/day. ``target_state`` remembers each target's
outcome so a retry only posts to targets not yet ok/skipped. NULL for existing rows: every
configured target is attempted once, exactly as before.

Revision ID: p17_student_sync_target_state
Revises: rs1_refresh_sessions
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "p17_student_sync_target_state"
down_revision = "rs1_refresh_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "student_sync_outbox",
        sa.Column("target_state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade():
    op.drop_column("student_sync_outbox", "target_state")
