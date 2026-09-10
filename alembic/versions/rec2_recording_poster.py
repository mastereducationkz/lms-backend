"""lesson recordings: poster image

Revision ID: rec2_recording_poster
Revises: rec1_lesson_recordings
Create Date: 2026-09-10

The Recordings library shows each lesson as a card with a preview. The ingest picks the most
detailed of several frames (slides rather than a webcam tile) and stores it beside the HLS as
``poster.jpg``; this column records that it exists. Additive and nullable: recordings ingested
before it simply have no preview until backfilled.
"""
from alembic import op
import sqlalchemy as sa


revision = "rec2_recording_poster"
down_revision = "rec1_lesson_recordings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("lesson_recordings", sa.Column("poster_url", sa.String(), nullable=True))


def downgrade():
    op.drop_column("lesson_recordings", "poster_url")
