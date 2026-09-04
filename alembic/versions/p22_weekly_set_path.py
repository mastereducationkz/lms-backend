"""platform_weekly_sets.set_path — the platform page of a weekly set, sent by the emitter
(SAT parity: paths come from the platform instead of LMS-side rules).

Revision ID: p22_weekly_set_path
Revises: p21_platform_diagnostics
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa


revision = "p22_weekly_set_path"
down_revision = "p21_platform_diagnostics"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("platform_weekly_sets", sa.Column("set_path", sa.String(), nullable=True))


def downgrade():
    op.drop_column("platform_weekly_sets", "set_path")
