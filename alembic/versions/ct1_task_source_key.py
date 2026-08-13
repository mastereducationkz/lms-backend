"""A durable identity for tasks another service asks us to create.

The CRM owns the freeze lifecycle; the LMS owns the curator task list. "Has this task already
been created?" is therefore a question asked across a network boundary that can retry, and the
answer has to survive the things that change about a task: its due date moves when a freeze is
extended, and its curator can be reassigned. Neither a title nor a
``(curator, student, date)`` tuple is an identity under those conditions. A key supplied by
the originator — ``freeze_return:{freeze_period_id}`` — is.

Uniqueness is enforced by the database rather than by the caller remembering to check first,
because the caller is a scheduler that may run concurrently in more than one worker.

Created with ``CONCURRENTLY`` so the index build does not lock the table against the running
application. That requires running outside a transaction, hence the autocommit block; the
``IF NOT EXISTS`` makes a resumed run safe after an interruption, which a concurrent build
needs because a failed one leaves an INVALID index behind.

Rollback: ``alembic downgrade -1`` drops the index and the column. No data is lost — the
column is additive and every existing row keeps ``NULL``, which the partial index ignores.

Revision ID: ct1_task_source_key
Revises: el2_email_log_content
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ct1_task_source_key"
down_revision: Union[str, Sequence[str], None] = "el2_email_log_content"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "curator_task_instances", sa.Column("source_key", sa.String(length=128), nullable=True)
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Partial: only externally-created tasks carry a key, and every locally-created task
        # would otherwise collide on NULL in some engines. It also keeps the index small.
        with op.get_context().autocommit_block():
            op.execute(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "
                "uq_curator_task_source_key ON curator_task_instances (source_key) "
                "WHERE source_key IS NOT NULL"
            )
    else:  # SQLite and friends, used by the test suite
        op.create_index(
            "uq_curator_task_source_key", "curator_task_instances", ["source_key"], unique=True
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS uq_curator_task_source_key")
    else:
        op.drop_index("uq_curator_task_source_key", table_name="curator_task_instances")
    op.drop_column("curator_task_instances", "source_key")
