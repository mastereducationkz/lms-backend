"""transcript words: each word with its time, so lines break where the speaker changes

Revision ID: ma5_transcript_words
Revises: ma4_talk_time
Create Date: 2026-09-12

One nullable column. Transcripts made before it keep working from their utterances.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "ma5_transcript_words"
down_revision = "ma4_talk_time"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("lesson_transcripts", sa.Column("words", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("lesson_transcripts", "words")
