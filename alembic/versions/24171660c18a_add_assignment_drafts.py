"""add assignment_drafts

Isolated server-side autosave of a student's in-progress answers for an
assignment. Never counted as a submission; deleted on successful submit.
One draft per (assignment, user).

Revision ID: 24171660c18a
Revises: ca1_crm_audit_outbox
Create Date: 2026-08-17 21:35:36.400679

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '24171660c18a'
down_revision: Union[str, Sequence[str], None] = 'ca1_crm_audit_outbox'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assignment_drafts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("answers", sa.Text(), nullable=True),
        sa.Column("file_url", sa.String(), nullable=True),
        sa.Column("submitted_file_name", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("assignment_id", "user_id", name="uq_assignment_draft_user"),
    )
    op.create_index(
        op.f("ix_assignment_drafts_id"), "assignment_drafts", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_assignment_drafts_assignment_id"), "assignment_drafts", ["assignment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assignment_drafts_user_id"), "assignment_drafts", ["user_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_assignment_drafts_user_id"), table_name="assignment_drafts")
    op.drop_index(op.f("ix_assignment_drafts_assignment_id"), table_name="assignment_drafts")
    op.drop_index(op.f("ix_assignment_drafts_id"), table_name="assignment_drafts")
    op.drop_table("assignment_drafts")
