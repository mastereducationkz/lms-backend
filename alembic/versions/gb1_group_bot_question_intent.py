"""telegram_group_questions.intent — which answer the group bot gave

Revision ID: gb1_group_bot_question_intent
Revises: av1_teacher_resubmission_policy
"""
import sqlalchemy as sa
from alembic import op

revision = "gb1_group_bot_question_intent"
down_revision = "av1_teacher_resubmission_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_group_questions", sa.Column("intent", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_group_questions", "intent")
