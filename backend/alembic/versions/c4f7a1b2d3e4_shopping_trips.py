"""shopping trips and per-trip checklist items

Revision ID: c4f7a1b2d3e4
Revises: a3f1c9d0e2b5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4f7a1b2d3e4"
down_revision: Union[str, Sequence[str], None] = "a3f1c9d0e2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("shopping_trips"):
        op.create_table(
            "shopping_trips",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("store_code", sa.String(), nullable=True),
            sa.Column("status", sa.Enum("active", "completed", "cancelled", name="shoppingtripstatus"),
                      nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_shopping_trips_status", "shopping_trips", ["status"])
    if not _has_table("shopping_trip_items"):
        op.create_table(
            "shopping_trip_items",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("trip_id", sa.String(), nullable=False),
            sa.Column("shopping_list_item_id", sa.String(), nullable=False),
            sa.Column("raw_text", sa.String(), nullable=False),
            sa.Column("qty", sa.Numeric(12, 3), nullable=False, server_default="1"),
            sa.Column("unit", sa.String(), nullable=True),
            sa.Column("note", sa.String(), nullable=True),
            sa.Column("status", sa.Enum("planned", "in_cart", "bought", "skipped",
                                         name="shoppingtripitemstatus"),
                      nullable=False, server_default="planned"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_shopping_trip_items_trip_id", "shopping_trip_items", ["trip_id"])
        op.create_index("ix_shopping_trip_items_list_id", "shopping_trip_items", ["shopping_list_item_id"])
        op.create_index("ix_shopping_trip_items_status", "shopping_trip_items", ["status"])
    if not _has_table("shopping_routines"):
        op.create_table(
            "shopping_routines",
            sa.Column("normalized_name", sa.String(), primary_key=True),
            sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("postponed_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("interval_days_override", sa.Numeric(8, 2), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has_table("shopping_trip_items"):
        op.drop_table("shopping_trip_items")
    if _has_table("shopping_trips"):
        op.drop_table("shopping_trips")
    if _has_table("shopping_routines"):
        op.drop_table("shopping_routines")
