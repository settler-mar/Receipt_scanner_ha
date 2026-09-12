"""telegram incoming message journal

Revision ID: c8a5d1e7f204
Revises: b7e4c2d9a610
Create Date: 2026-09-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8a5d1e7f204"
down_revision: Union[str, Sequence[str], None] = "b7e4c2d9a610"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Base.metadata.create_all() runs before Alembic in this application and
    # therefore already creates this table on fresh/current installations.
    if "telegram_message_log" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "telegram_message_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_type", sa.String(), nullable=True),
        sa.Column("chat_title", sa.String(), nullable=True),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_username", sa.String(), nullable=True),
        sa.Column("sender_display_name", sa.String(), nullable=True),
        sa.Column("sender_is_bot", sa.Boolean(), nullable=False),
        sa.Column("message_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_key", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("media_meta", sa.JSON(), nullable=False),
        sa.Column("authorized", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_update_id", name="uq_telegram_message_log_update"),
    )
    op.create_index("ix_telegram_message_log_chat_id", "telegram_message_log", ["chat_id"])
    op.create_index("ix_telegram_message_log_sender_id", "telegram_message_log", ["sender_id"])
    op.create_index("ix_telegram_message_log_message_thread_id", "telegram_message_log", ["message_thread_id"])
    op.create_index("ix_telegram_message_log_status", "telegram_message_log", ["status"])
    op.create_index("ix_telegram_message_log_received_at", "telegram_message_log", ["received_at"])
    op.create_index("ix_telegram_message_log_conversation_key", "telegram_message_log", ["conversation_key"])
    op.create_index(
        "ix_telegram_message_log_conversation", "telegram_message_log",
        ["conversation_key", "received_at"],
    )


def downgrade() -> None:
    if "telegram_message_log" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("telegram_message_log")
