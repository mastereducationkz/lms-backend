"""Add self-hosted HLS video fields + video_ingest_jobs queue

Revision ID: p2_video_hls
Revises: p1_message_file_url
Create Date: 2026-07-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'p2_video_hls'
down_revision = 'p1_message_file_url'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('steps', sa.Column('hls_url', sa.String(), nullable=True))
    op.add_column('steps', sa.Column('hls_url_en', sa.String(), nullable=True))
    op.add_column('steps', sa.Column('video_status', sa.String(length=16), nullable=True))

    op.create_table(
        'video_ingest_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('step_id', sa.Integer(), nullable=False),
        sa.Column('lang', sa.String(length=8), nullable=False, server_default='ru'),
        sa.Column('youtube_url', sa.String(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['step_id'], ['steps.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('step_id', 'lang', name='uq_video_ingest_step_lang'),
    )
    op.create_index('idx_video_ingest_status', 'video_ingest_jobs', ['status'])
    op.create_index(op.f('ix_video_ingest_jobs_step_id'), 'video_ingest_jobs', ['step_id'])
    op.create_index(op.f('ix_video_ingest_jobs_id'), 'video_ingest_jobs', ['id'])


def downgrade():
    op.drop_index(op.f('ix_video_ingest_jobs_id'), table_name='video_ingest_jobs')
    op.drop_index(op.f('ix_video_ingest_jobs_step_id'), table_name='video_ingest_jobs')
    op.drop_index('idx_video_ingest_status', table_name='video_ingest_jobs')
    op.drop_table('video_ingest_jobs')
    op.drop_column('steps', 'video_status')
    op.drop_column('steps', 'hls_url_en')
    op.drop_column('steps', 'hls_url')
