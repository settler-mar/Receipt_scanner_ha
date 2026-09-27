"""add editable AI prompt catalogue and revision history

Revision ID: 9b7c6d5e4f3a
Revises: f3a4b5c6d7e8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9b7c6d5e4f3a"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ai_prompts" not in inspector.get_table_names():
        op.create_table(
            "ai_prompts",
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("task", sa.String(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "ai_prompt_revisions" not in inspector.get_table_names():
        op.create_table(
            "ai_prompt_revisions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("prompt_key", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("changed_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    existing_indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("ai_prompt_revisions")}
    if "ix_ai_prompt_revisions_prompt_key" not in existing_indexes:
        op.create_index("ix_ai_prompt_revisions_prompt_key", "ai_prompt_revisions", ["prompt_key"])
    if "ix_ai_prompt_revisions_created_at" not in existing_indexes:
        op.create_index("ix_ai_prompt_revisions_created_at", "ai_prompt_revisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_prompt_revisions_created_at", table_name="ai_prompt_revisions")
    op.drop_index("ix_ai_prompt_revisions_prompt_key", table_name="ai_prompt_revisions")
    op.drop_table("ai_prompt_revisions")
    op.drop_table("ai_prompts")
