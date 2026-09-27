"""add receipt/list match candidates

Revision ID: c7d0e4f6a9b1
Revises: c6b9c3e5f7a8
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "c7d0e4f6a9b1"
down_revision: Union[str, Sequence[str], None] = "c6b9c3e5f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "shopping_match_candidates" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "shopping_match_candidates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("shopping_list_item_id", sa.String(), nullable=False),
        sa.Column("receipt_item_id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.String(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("explanation", sa.String(), nullable=True),
        sa.Column("status", sa.Enum("pending", "confirmed", "rejected", name="shoppingmatchstatus"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_shopping_match_candidates_shopping_list_item_id", "shopping_match_candidates", ["shopping_list_item_id"])
    op.create_index("ix_shopping_match_candidates_receipt_item_id", "shopping_match_candidates", ["receipt_item_id"])
    op.create_index("ix_shopping_match_candidates_receipt_id", "shopping_match_candidates", ["receipt_id"])
    op.create_index("ix_shopping_match_candidates_status", "shopping_match_candidates", ["status"])


def downgrade() -> None:
    op.drop_table("shopping_match_candidates")
