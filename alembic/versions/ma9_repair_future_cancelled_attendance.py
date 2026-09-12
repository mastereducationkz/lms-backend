"""repair cancelled attendance on active future lessons

Revision ID: ma9_fix_cancelled_attendance
Revises: ma8_telegram_homework_notices
Create Date: 2026-09-12

``cancelled`` is an event-level fact.  Before this migration, the attendance
grid allowed it to be saved per student, so an active scheduled lesson could
look cancelled for only some (or all) of its roster.  Future lessons cannot
have attendance yet; restore those bad rows to the neutral ``registered``
state without cancelling or moving the lesson itself.
"""
from alembic import op


revision = "ma9_fix_cancelled_attendance"
down_revision = "ma8_telegram_homework_notices"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        """
        UPDATE attendances AS attendance
        SET status = 'registered', score = 0
        FROM events AS event
        WHERE attendance.event_id = event.id
          AND attendance.status = 'cancelled'
          AND event.is_active IS TRUE
          AND event.start_datetime > (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
        """
    )


def downgrade():
    # The prior ``cancelled`` values were invalid and indistinguishable from a
    # genuine neutral attendance row, so a downgrade deliberately does not
    # recreate bad data.
    pass
