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
Тот же сторож расширен и на ``excuse_note``: правка только текста причины при уже стоящем
флаге тоже обязана дойти до CRM, а не оставить у неё устаревшую формулировку.

Downgrade сносит только объект посещаемости (``trg_crm_audit_attendance`` /
``crm_audit_enqueue_attendance``), а не все четыре: эта ревизия не трогала group/member/
event, и полный ``uninstall_sql()`` унёс бы их без причины. Восстанавливать здесь текст
старого тела не вариант — module.installed_sql() отдаёт только текущее тело, а хранить
устаревшую копию SQL в файле ревизии значит гарантированно разойтись с модулем при
следующей правке. Снять триггер целиком безопаснее: следующий ``downgrade`` (``exc1``)
дропает сами колонки ``excused``/``excuse_note``, и к этому моменту ничего в базе на них
уже не ссылается.
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
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    from src.crm_audit.triggers import ATTENDANCE_FUNCTION, ATTENDANCE_TRIGGER

    # Scoped drop, not the module's uninstall_sql(): that removes all four trigger/function
    # pairs, but this revision only ever installed the attendance one. Dropping it (rather
    # than trying to reinstall whatever body predated this revision — the module only holds
    # the current one) leaves nothing referencing attendances.excused / excuse_note, so the
    # next downgrade (exc1, which drops those columns) lands on a consistent database.
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {ATTENDANCE_TRIGGER} ON attendances;"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {ATTENDANCE_FUNCTION}();"))
