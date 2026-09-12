"""shopping list tables (ROADMAP.md Крок 2)

Revision ID: b2f1a7c9d3e4
Revises: b2c1d4e5f6a7
Create Date: 2026-08-29

Chained after Крок 1's migration (b2c1d4e5f6a7) so `alembic upgrade head` stays
linear. If Крок 1's revision id changes before merge, update `down_revision`
here to match.

The create_table calls are guarded by an existence check: on a fresh database
`Base.metadata.create_all` (app/main.py) already builds these tables once the
models module is imported, so the migration must be a no-op there and only do
real work on an existing database that predates Крок 2.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2f1a7c9d3e4"
down_revision: Union[str, Sequence[str], None] = "b2c1d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATUS = sa.Enum("pending", "bought", "cancelled", name="shoppingliststatus")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("shopping_list_items"):
        op.create_table(
            "shopping_list_items",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("raw_text", sa.String(), nullable=False),
            sa.Column("normalized_name", sa.String(), nullable=False, server_default=""),
            sa.Column("category_id", sa.String(), nullable=True),
            sa.Column("barcode", sa.String(), nullable=True),
            sa.Column("qty", sa.Numeric(12, 3), nullable=False, server_default="1"),
            sa.Column("unit", sa.String(), nullable=True),
            sa.Column("status", _STATUS, nullable=False, server_default="pending"),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("auto_matched", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("bought_receipt_item_id", sa.String(), nullable=True),
            sa.Column("rejected_receipt_item_id", sa.String(), nullable=True),
            sa.Column("added_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("bought_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_shopping_list_items_normalized_name", "shopping_list_items", ["normalized_name"])
        op.create_index("ix_shopping_list_items_status", "shopping_list_items", ["status"])

    if not _has_table("product_aliases"):
        op.create_table(
            "product_aliases",
            sa.Column("normalized_phrase", sa.String(), primary_key=True),
            sa.Column("store_code", sa.String(), primary_key=True),
            sa.Column("query", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_table("product_aliases"):
        op.drop_table("product_aliases")
    if _has_table("shopping_list_items"):
        op.drop_index("ix_shopping_list_items_status", table_name="shopping_list_items")
        op.drop_index("ix_shopping_list_items_normalized_name", table_name="shopping_list_items")
        op.drop_table("shopping_list_items")
