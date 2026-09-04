"""lessons.kind — distinguishes checkpoint assessments from ordinary course units.

Revision ID: cp2_lesson_kind
Revises: cp1_checkpoints
Create Date: 2026-09-04
"""
from alembic import op
import sqlalchemy as sa


revision = "cp2_lesson_kind"
down_revision = "cp1_checkpoints"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("lessons", sa.Column("kind", sa.String(length=16), nullable=False,
                                       server_default="unit"))
    op.create_index("ix_lessons_kind", "lessons", ["kind"])
    # Anything an earlier seed already created as a checkpoint quiz lesson.
    op.execute(
        "UPDATE lessons SET kind = 'checkpoint' WHERE id IN "
        "(SELECT quiz_lesson_id FROM checkpoint_definitions WHERE quiz_lesson_id IS NOT NULL)"
    )


def downgrade():
    op.drop_index("ix_lessons_kind", table_name="lessons")
    op.drop_column("lessons", "kind")
