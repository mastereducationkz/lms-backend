"""crm_audit: триггер посещаемости замечает уважительность

Revision ID: exc2_attendance_excused_trigger
Revises: exc1_excused_absence
Create Date: 2026-09-16

Тело триггера живёт в ``src/crm_audit/triggers.py`` и ставится оттуда — так же, как его
поставила ``ca1_crm_audit_outbox``. Правка модуля меняет то, что получит чистая база, но
уже развёрнутой не делает ничего: функция в Postgres — отдельный объект, и пока кто-то не
выполнит ``CREATE OR REPLACE`` заново, прод продолжает считать по старому телу. Ревизия
существует ровно для этого — перевыполнить ``install_sql()``.

Установка идемпотентна целиком (``CREATE OR REPLACE FUNCTION`` + ``DROP TRIGGER IF
EXISTS``), поэтому переставляются все четыре триггера, а не один: так тело в базе гарантированно
совпадает с модулем, вместо того чтобы расходиться по одному объекту за ревизию.

Что изменилось в теле: UPDATE, меняющий только ``attendances.excused``, раньше возвращал
NULL — статус не менялся, события не было. Именно этот переход и есть деньги: уважительная,
поставленная на уже списанном уроке, обязана дойти до CRM, иначе возврат не случится.
"""
from alembic import op
import sqlalchemy as sa


revision = "exc2_attendance_excused_trigger"
down_revision = "exc1_excused_absence"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    # Триггеры — только PostgreSQL, ровно как в ca1_crm_audit_outbox.
    if bind.dialect.name != "postgresql":
        return
    from src.crm_audit.triggers import install_sql

    op.execute(sa.text(install_sql()))


def downgrade():
    # Откатывать нечего: предыдущее тело триггера восстанавливается откатом самой ревизии,
    # которая его написала. Дублировать здесь устаревшую копию SQL — гарантированный
    # источник расхождения между файлом и модулем.
    pass
