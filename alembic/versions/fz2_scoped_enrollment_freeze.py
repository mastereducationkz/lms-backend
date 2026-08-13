"""Freeze one enrollment, not one student.

A student studies several products at once. Keyed on the student alone, the mirror could only
say "frozen" or "not frozen", so a SAT freeze suppressed the student's IELTS attendance and
homework and hung a global «Заморожен» badge on somebody who was in class that morning. The
scope has to be the group, and the primary key has to say so.

``group_id = 0`` is the sentinel for "student-wide", and it is why this migration is safe.
Every existing row gets 0 and therefore keeps its exact current meaning — it freezes
everything — so the LMS answers precisely as it did before until the CRM starts sending
scoped rows. The column is NOT NULL because a nullable column cannot be part of a primary
key, which is the only reason a sentinel is used instead of NULL.

``scope_group_name`` and ``scope_product`` are snapshots the CRM takes at freeze time: the
membership they are read from is deleted moments later, so there is nothing left to join to
afterwards. They are staff labels and are never shown to students.

``status`` grows from 16 to 24 characters because the new ``pending_placement`` — returned,
but not yet put in a group — is seventeen. Without this the very first delivery of that
status is rejected by the database rather than mirrored.

Additive and reversible. Shipping it before the CRM sends a single scoped row changes no
answer the LMS gives.

This revision and ``ct1_task_source_key`` were written in parallel and both originally
branched off ``el2_email_log_content``. That is two heads, and two heads is not a failing
test — the container runs ``alembic upgrade head`` before it boots, so it is a deploy that
dies. ``ct1`` landed on main first, so this one chains onto it. Neither could have named the
other while the other was unmerged; the answer only became knowable at the merge, which is
where it was applied.

Revision ID: fz2_scoped_enrollment_freeze
Revises: ct1_task_source_key
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fz2_scoped_enrollment_freeze"
down_revision: Union[str, Sequence[str], None] = "ct1_task_source_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Postgres' default name for the constraint ``sa.PrimaryKeyConstraint("user_id")`` created.
_PK = "student_freeze_state_pkey"


def upgrade() -> None:
    op.add_column(
        "student_freeze_state",
        sa.Column("group_id", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "student_freeze_state", sa.Column("scope_group_name", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "student_freeze_state", sa.Column("scope_product", sa.String(length=128), nullable=True)
    )

    if op.get_bind().dialect.name == "sqlite":
        # SQLite can alter neither a primary key nor a column type, and nothing needs it to:
        # the test suite builds this schema with ``Base.metadata.create_all()``, which reads
        # both straight off the model.
        return

    op.alter_column(
        "student_freeze_state",
        "status",
        type_=sa.String(length=24),
        existing_type=sa.String(length=16),
        existing_nullable=False,
        existing_server_default="active",
    )
    op.drop_constraint(_PK, "student_freeze_state", type_="primary")
    op.create_primary_key(_PK, "student_freeze_state", ["user_id", "group_id"])


def downgrade() -> None:
    # Scoped rows have no representation in the one-row-per-student shape. Collapsing two
    # freezes onto one student would either violate the restored primary key or silently keep
    # one product's freeze and drop the other's, which is worse than saying nothing: deleting
    # them makes the LMS answer "not frozen", and the CRM re-sends every live scope on its
    # next sweep. Must run before the key is restored, or the key cannot be created.
    op.execute("DELETE FROM student_freeze_state WHERE group_id <> 0")

    if op.get_bind().dialect.name != "sqlite":
        # 'pending_placement' does not fit in 16 characters, so it has to go before the column
        # shrinks or the ALTER fails. 'resumed' is the nearest word the old vocabulary has:
        # both mean the freeze is over and the student is not suppressed.
        op.execute(
            "UPDATE student_freeze_state SET status = 'resumed' "
            "WHERE status = 'pending_placement'"
        )
        op.alter_column(
            "student_freeze_state",
            "status",
            type_=sa.String(length=16),
            existing_type=sa.String(length=24),
            existing_nullable=False,
            existing_server_default="active",
        )
        op.drop_constraint(_PK, "student_freeze_state", type_="primary")
        op.create_primary_key(_PK, "student_freeze_state", ["user_id"])

    op.drop_column("student_freeze_state", "scope_product")
    op.drop_column("student_freeze_state", "scope_group_name")
    op.drop_column("student_freeze_state", "group_id")
