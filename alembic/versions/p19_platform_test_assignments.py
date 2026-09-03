"""Platform-test assignments (Platform Integration Pack §6.3, E1).

- platform_test_assignments: links one auto-created ``platform_test`` Assignment to its weekly
  set and group.
- groups.platform_tests_opt_out: per-group opt-out (weekly sets are global on IELTS).
- platform_weekly_sets.date_from/date_to widen from DATE to TIMESTAMP: the set window is a
  full timestamp on the platform (the AI Speaking part closes at date_to's exact minute).

Revision ID: p19_platform_test_assignments
Revises: p18_platform_events
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa


revision = "p19_platform_test_assignments"
down_revision = "p18_platform_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "groups",
        sa.Column("platform_tests_opt_out", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.alter_column("platform_weekly_sets", "date_from", type_=sa.DateTime(), existing_type=sa.Date(),
                    existing_nullable=True, postgresql_using="date_from::timestamp")
    op.alter_column("platform_weekly_sets", "date_to", type_=sa.DateTime(), existing_type=sa.Date(),
                    existing_nullable=True, postgresql_using="date_to::timestamp")
    op.create_table(
        "platform_test_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("assignment_id", sa.Integer(), sa.ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("weekly_set_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id", name="uq_platform_test_assignments_assignment"),
        sa.UniqueConstraint("platform", "weekly_set_id", "group_id", name="uq_platform_test_assignments_set_group"),
    )
    op.create_index("ix_platform_test_assignments_group", "platform_test_assignments", ["group_id"])


def downgrade():
    op.drop_index("ix_platform_test_assignments_group", table_name="platform_test_assignments")
    op.drop_table("platform_test_assignments")
    op.alter_column("platform_weekly_sets", "date_to", type_=sa.Date(), existing_type=sa.DateTime(),
                    existing_nullable=True, postgresql_using="date_to::date")
    op.alter_column("platform_weekly_sets", "date_from", type_=sa.Date(), existing_type=sa.DateTime(),
                    existing_nullable=True, postgresql_using="date_from::date")
    op.drop_column("groups", "platform_tests_opt_out")
