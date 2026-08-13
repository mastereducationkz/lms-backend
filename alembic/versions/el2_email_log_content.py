"""Store what was actually sent — except when it is a credential.

"What did the email say?" is the second question anybody asks about a delivery, after "did
it arrive", and the journal could not answer it. These three columns do.

``content_withheld`` is not redundant with ``body_html IS NULL``. They mean different things
and the journal must never conflate them:

* ``content_withheld = true`` — we deliberately did not store this, because the mail carries
  a password or a reset link.
* ``content_withheld = false`` **and** no body — this row predates content capture. Nothing
  is being hidden; there is simply nothing to show.

Backfilled as ``false`` for every existing row, which is the honest value: those rows are the
second case, not the first. No historical content is invented.

Revision ID: el2_email_log_content
Revises: el1_email_log
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "el2_email_log_content"
down_revision: Union[str, Sequence[str], None] = "el1_email_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("email_log", sa.Column("body_html", sa.Text(), nullable=True))
    op.add_column("email_log", sa.Column("body_text", sa.Text(), nullable=True))
    op.add_column(
        "email_log",
        sa.Column(
            "content_withheld",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_log", "content_withheld")
    op.drop_column("email_log", "body_text")
    op.drop_column("email_log", "body_html")
