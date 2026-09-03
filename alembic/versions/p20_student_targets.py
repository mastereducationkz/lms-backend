"""Structured per-track student targets (Platform Integration Pack §6.4, E5).

Creates ``student_targets`` and migrates the legacy free-text IELTS target from
``assignment_zero_submissions.ielts_target_score``: values that parse as a band in 0.5 steps
between 4.0 and 9.0 become ``{"overall": band}``; anything else is kept verbatim in ``note``
(nothing is dropped). Idempotent: rows that already have an ielts target are left alone.

Revision ID: p20_student_targets
Revises: p19_platform_test_assignments
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "p20_student_targets"
down_revision = "p19_platform_test_assignments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "student_targets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track", sa.String(length=16), nullable=False),
        sa.Column("targets", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="student"),
        sa.Column("set_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "track", name="uq_student_targets_user_track"),
    )
    op.create_index("ix_student_targets_user", "student_targets", ["user_id"])

    # Data migration of the legacy free-text IELTS target (same rule as src.integrations.targets.parse_band).
    op.execute(
        """
        INSERT INTO student_targets (user_id, track, targets, note, source, set_by, created_at, updated_at)
        SELECT s.user_id, 'ielts',
               CASE WHEN b.band IS NOT NULL THEN jsonb_build_object('overall', b.band) ELSE '{}'::jsonb END,
               CASE WHEN b.band IS NULL THEN s.ielts_target_score ELSE NULL END,
               'assignment_zero', NULL, (now() at time zone 'utc'), (now() at time zone 'utc')
        FROM assignment_zero_submissions s
        CROSS JOIN LATERAL (
            SELECT CASE
                WHEN v ~ '^[4-9](\\.[05])?$' AND v::numeric BETWEEN 4.0 AND 9.0 THEN v::numeric
                ELSE NULL
            END AS band
            FROM (SELECT regexp_replace(regexp_replace(lower(btrim(s.ielts_target_score)), '^band\\s*', ''), '[+,]', '.', 'g') AS v) t
        ) b
        WHERE COALESCE(btrim(s.ielts_target_score), '') <> ''
          AND NOT EXISTS (SELECT 1 FROM student_targets st WHERE st.user_id = s.user_id AND st.track = 'ielts')
        """
    )


def downgrade():
    op.drop_index("ix_student_targets_user", table_name="student_targets")
    op.drop_table("student_targets")
