"""meet flag reviews: a Meet attendance flag looked at, with the reason, so it stops asking

Revision ID: ma3_meet_flag_reviews
Revises: ma2_recording_watch_links
Create Date: 2026-09-11

One row per (lesson, person, flag): who reviewed it, when, and why — required where the mark
contradicts the room, optional for lateness. One new table, nothing else touched.
"""
from alembic import op
import sqlalchemy as sa


revision = "ma3_meet_flag_reviews"
down_revision = "ma2_recording_watch_links"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "meet_flag_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(40), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("event_id", "user_id", "code", name="uq_meet_flag_review"),
    )
    op.create_index("ix_meet_flag_reviews_id", "meet_flag_reviews", ["id"])
    op.create_index("ix_meet_flag_reviews_event_id", "meet_flag_reviews", ["event_id"])


def downgrade():
    op.drop_index("ix_meet_flag_reviews_event_id", table_name="meet_flag_reviews")
    op.drop_index("ix_meet_flag_reviews_id", table_name="meet_flag_reviews")
    op.drop_table("meet_flag_reviews")
