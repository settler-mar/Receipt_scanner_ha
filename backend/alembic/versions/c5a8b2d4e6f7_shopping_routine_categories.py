"""ignore regular-purchase recommendations by category

Revision ID: c5a8b2d4e6f7
Revises: c4f7a1b2d3e4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5a8b2d4e6f7"
down_revision: Union[str, Sequence[str], None] = "c4f7a1b2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("shopping_routine_categories"):
        op.create_table(
            "shopping_routine_categories",
            sa.Column("category_id", sa.String(), primary_key=True),
            sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _has_table("shopping_product_aliases"):
        op.create_table(
            "shopping_product_aliases",
            sa.Column("alias_name", sa.String(), primary_key=True),
            sa.Column("canonical_name", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=False),
            sa.Column("category_id", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=False, server_default="rule"),
            sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_shopping_product_aliases_canonical", "shopping_product_aliases", ["canonical_name"])
        op.create_index("ix_shopping_product_aliases_category", "shopping_product_aliases", ["category_id"])


def downgrade() -> None:
    if _has_table("shopping_product_aliases"):
        op.drop_table("shopping_product_aliases")
    if _has_table("shopping_routine_categories"):
        op.drop_table("shopping_routine_categories")
