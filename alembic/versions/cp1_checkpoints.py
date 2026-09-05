"""SAT Checkpoints: definitions, required units, per-student rows, per-group flags.

Revision ID: cp1_checkpoints
Revises: p22_weekly_set_path
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "cp1_checkpoints"
down_revision = "p22_weekly_set_path"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("groups", sa.Column("checkpoints_enabled", sa.Boolean(),
                                      server_default=sa.text("false"), nullable=False))
    op.add_column("groups", sa.Column("checkpoints_start_number", sa.Integer(),
                                      server_default="1", nullable=False))

    op.create_table(
        "checkpoint_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("quiz_lesson_id", sa.Integer(), sa.ForeignKey("lessons.id", ondelete="SET NULL"), nullable=True),
        sa.Column("total_questions", sa.Integer(), nullable=False, server_default="45"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("course_id", "number", name="uq_checkpoint_course_number"),
    )
    op.create_index("ix_checkpoint_definitions_course_id", "checkpoint_definitions", ["course_id"])

    op.create_table(
        "checkpoint_required_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("checkpoint_id", sa.Integer(),
                  sa.ForeignKey("checkpoint_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.Integer(), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("checkpoint_id", "lesson_id", name="uq_checkpoint_required_unit"),
    )
    op.create_index("ix_checkpoint_required_units_checkpoint_id", "checkpoint_required_units", ["checkpoint_id"])
    op.create_index("ix_checkpoint_required_units_lesson_id", "checkpoint_required_units", ["lesson_id"])

    op.create_table(
        "student_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkpoint_id", sa.Integer(),
                  sa.ForeignKey("checkpoint_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("checkpoint_number", sa.Integer(), nullable=False),
        sa.Column("required_unit_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="locked"),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("deadline", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("quiz_attempt_id", sa.Integer(), sa.ForeignKey("quiz_attempts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("correct_answers", sa.Integer(), nullable=True),
        sa.Column("total_questions", sa.Integer(), nullable=True),
        sa.Column("percentage", sa.Float(), nullable=True),
        sa.Column("opened_by", sa.String(length=16), nullable=True),
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("student_id", "group_id", "checkpoint_id", name="uq_student_checkpoint"),
    )
    op.create_index("ix_student_checkpoints_student_id", "student_checkpoints", ["student_id"])
    op.create_index("ix_student_checkpoints_group_id", "student_checkpoints", ["group_id"])
    op.create_index("ix_student_checkpoints_checkpoint_id", "student_checkpoints", ["checkpoint_id"])
    op.create_index("ix_student_checkpoints_group_checkpoint", "student_checkpoints", ["group_id", "checkpoint_id"])


def downgrade():
    op.drop_table("student_checkpoints")
    op.drop_table("checkpoint_required_units")
    op.drop_table("checkpoint_definitions")
    op.drop_column("groups", "checkpoints_start_number")
    op.drop_column("groups", "checkpoints_enabled")
