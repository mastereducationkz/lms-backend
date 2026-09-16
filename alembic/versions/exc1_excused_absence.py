"""attendances: уважительный пропуск — флаг, причина, кто и когда

Revision ID: exc1_excused_absence
Revises: rec1_lesson_recordings
Create Date: 2026-09-16

Написано руками, а не автогенерацией: autogenerate на этом проекте стабильно вытаскивает
постороннюю дрифт-разницу от моделей, созданных через ``create_all()`` и никогда не
мигрированных. Всё здесь аддитивно — четыре колонки и один constraint, без бэкфилла и без
переписывания таблицы, — поэтому применимо на работающем приложении.

Почему не новое значение ``attendances.status``: см. ``src/services/attendance_status.py``.
Коротко — около пятнадцати мест сравнивают статус литералами, и новое значение молча выпало
бы из «урок отмечен», а с ним из списания и из отчётов.
"""
from alembic import op
import sqlalchemy as sa


revision = "exc1_excused_absence"
down_revision = "rec1_lesson_recordings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "attendances",
        sa.Column(
            "excused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("attendances", sa.Column("excuse_note", sa.Text(), nullable=True))
    op.add_column(
        "attendances", sa.Column("excused_by_user_id", sa.Integer(), nullable=True)
    )
    op.add_column("attendances", sa.Column("excused_at", sa.DateTime(), nullable=True))
    op.create_foreign_key(
        "fk_attendances_excused_by_user",
        "attendances",
        "users",
        ["excused_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_attendance_excused_has_note",
        "attendances",
        "NOT excused OR (excuse_note IS NOT NULL AND length(trim(excuse_note)) > 0)",
    )


def downgrade():
    op.drop_constraint("ck_attendance_excused_has_note", "attendances", type_="check")
    op.drop_constraint("fk_attendances_excused_by_user", "attendances", type_="foreignkey")
    op.drop_column("attendances", "excused_at")
    op.drop_column("attendances", "excused_by_user_id")
    op.drop_column("attendances", "excuse_note")
    op.drop_column("attendances", "excused")
