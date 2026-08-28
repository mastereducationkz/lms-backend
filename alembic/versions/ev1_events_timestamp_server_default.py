"""Add server_default now() to events.created_at/updated_at.

CRM writes the LMS ``events`` table directly (add-lesson / retroactive attendance).
Those inserts historically bypassed the ORM's Python-side ``default=``, leaving
``created_at``/``updated_at`` NULL. A NULL timestamp then fails the required
``datetime`` fields of ``EventSchema``, 500-ing the entire /events/calendar month
and blanking affected teachers' calendars. A DB-level server default backstops any
non-ORM insert path. Existing NULLs are backfilled here as well.

Revision ID: ev1_events_timestamp_server_default
Revises: mg1_merge_drafts_freeze
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ev1_events_timestamp_server_default"
down_revision: Union[str, Sequence[str], None] = "mg1_merge_drafts_freeze"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfill any rows that slipped in NULL before the default existed.
    op.execute(
        """
        UPDATE events
        SET created_at = COALESCE(created_at, start_datetime),
            updated_at = COALESCE(updated_at, created_at, start_datetime)
        WHERE created_at IS NULL OR updated_at IS NULL
        """
    )
    op.alter_column("events", "created_at", server_default=sa.text("now()"))
    op.alter_column("events", "updated_at", server_default=sa.text("now()"))


def downgrade() -> None:
    op.alter_column("events", "created_at", server_default=None)
    op.alter_column("events", "updated_at", server_default=None)
