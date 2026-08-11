"""add curator_onboarding table + backfill existing pairs to done

Revision ID: co1_curator_onboarding
Revises: ex3_planned_date_stub
"""
from alembic import op
import sqlalchemy as sa

revision = "co1_curator_onboarding"
down_revision = "ex3_planned_date_stub"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "curator_onboarding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("curator_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Integer(),
                  sa.ForeignKey("groups.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("curator_id", "student_id",
                            name="uq_curator_onboarding_pair"),
    )
    op.create_index("ix_curator_onboarding_curator_id", "curator_onboarding", ["curator_id"])
    op.create_index("ix_curator_onboarding_student_id", "curator_onboarding", ["student_id"])
    op.create_index("ix_curator_onboarding_curator_status", "curator_onboarding",
                    ["curator_id", "status"])

    # --- Launch baseline: seed all existing active pairs as done (start clean) ---
    op.execute(
        """
        INSERT INTO curator_onboarding
            (curator_id, student_id, group_id, status, created_at, completed_at)
        SELECT DISTINCT ON (g.curator_id, gs.student_id)
               g.curator_id, gs.student_id, g.id, 'done', now(), now()
        FROM groups g
        JOIN group_students gs ON gs.group_id = g.id
        JOIN users u ON u.id = gs.student_id
        WHERE g.is_active = true
          AND g.curator_id IS NOT NULL
          AND u.is_active = true
        ORDER BY g.curator_id, gs.student_id, gs.created_at DESC
        """
    )


def downgrade():
    op.drop_index("ix_curator_onboarding_curator_status", table_name="curator_onboarding")
    op.drop_index("ix_curator_onboarding_student_id", table_name="curator_onboarding")
    op.drop_index("ix_curator_onboarding_curator_id", table_name="curator_onboarding")
    op.drop_table("curator_onboarding")
