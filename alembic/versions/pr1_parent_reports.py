"""parent_reports: сохранённые еженедельные отчёты родителям

Revision ID: pr1_parent_reports
Revises: td2_discipline_digest
Create Date: 2026-09-18

Написано руками, а не автогенерацией: autogenerate на этом проекте стабильно вытаскивает
постороннюю дрифт-разницу от моделей, созданных через ``create_all()``. Здесь одна новая
таблица, без бэкфилла и без изменения существующих, — применимо на работающем приложении.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "pr1_parent_reports"
down_revision = "td2_discipline_digest"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "parent_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("template_key", sa.String(length=8), nullable=False),
        sa.Column("template_auto", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("facts_json", postgresql.JSONB(), nullable=False),
        sa.Column("body_generated", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("curator_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["student_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("student_id", "week_start",
                            name="uq_parent_report_student_week"),
    )
    op.create_index("ix_parent_reports_student_id", "parent_reports", ["student_id"])
    op.create_index("ix_parent_reports_week_start", "parent_reports", ["week_start"])


def downgrade():
    op.drop_index("ix_parent_reports_week_start", table_name="parent_reports")
    op.drop_index("ix_parent_reports_student_id", table_name="parent_reports")
    op.drop_table("parent_reports")
