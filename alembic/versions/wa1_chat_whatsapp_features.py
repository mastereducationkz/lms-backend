"""chat WhatsApp features: reactions, reply, delivery/read receipts, conversation mute

Revision ID: wa1_chat_whatsapp_features
Revises: of1_official_name
Create Date: 2026-08-01 00:00:00.000000

Adds to the 1:1 messaging domain:
  * messages.reply_to_message_id  -> quote/reply (self-FK, ON DELETE SET NULL)
  * messages.delivered_at / read_at -> delivery + read receipts (ticks)
  * message_reactions             -> one emoji reaction per user per message
  * conversation_mutes            -> per-user, per-conversation notification mute

Historical rows that were already ``is_read`` are backfilled so their read ticks
render immediately (read_at/delivered_at = created_at).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "wa1_chat_whatsapp_features"
down_revision: Union[str, Sequence[str], None] = "of1_official_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- messages: reply + receipts -----------------------------------------
    op.add_column("messages", sa.Column("reply_to_message_id", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_messages_reply_to_message_id", "messages", ["reply_to_message_id"])
    op.create_foreign_key(
        "fk_messages_reply_to_message_id",
        "messages", "messages",
        ["reply_to_message_id"], ["id"],
        ondelete="SET NULL",
    )
    # Backfill receipts for already-read history so ticks render for old chats.
    op.execute(
        "UPDATE messages SET read_at = created_at, delivered_at = created_at "
        "WHERE is_read = true AND read_at IS NULL"
    )

    # --- message_reactions ---------------------------------------------------
    op.create_table(
        "message_reactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("emoji", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("message_id", "user_id", name="uq_message_reaction_user"),
    )
    op.create_index("ix_message_reactions_message_id", "message_reactions", ["message_id"])
    op.create_index("ix_message_reactions_user_id", "message_reactions", ["user_id"])

    # --- conversation_mutes --------------------------------------------------
    op.create_table(
        "conversation_mutes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["partner_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "partner_id", name="uq_conversation_mute"),
    )
    op.create_index("ix_conversation_mutes_user_id", "conversation_mutes", ["user_id"])
    op.create_index("ix_conversation_mutes_partner_id", "conversation_mutes", ["partner_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_mutes_partner_id", table_name="conversation_mutes")
    op.drop_index("ix_conversation_mutes_user_id", table_name="conversation_mutes")
    op.drop_table("conversation_mutes")

    op.drop_index("ix_message_reactions_user_id", table_name="message_reactions")
    op.drop_index("ix_message_reactions_message_id", table_name="message_reactions")
    op.drop_table("message_reactions")

    op.drop_constraint("fk_messages_reply_to_message_id", "messages", type_="foreignkey")
    op.drop_index("ix_messages_reply_to_message_id", table_name="messages")
    op.drop_column("messages", "read_at")
    op.drop_column("messages", "delivered_at")
    op.drop_column("messages", "reply_to_message_id")
