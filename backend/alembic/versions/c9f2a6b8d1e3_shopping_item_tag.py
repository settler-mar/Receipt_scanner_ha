"""add optional shopping-list item tag

Revision ID: c9f2a6b8d1e3
Revises: c8e1f5a7b0c2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9f2a6b8d1e3"
down_revision: Union[str, Sequence[str], None] = "c8e1f5a7b0c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "shopping_list_items" not in sa.inspect(bind).get_table_names():
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns("shopping_list_items")}
    if "tag" not in columns:
        op.add_column("shopping_list_items", sa.Column("tag", sa.String(), nullable=True))
        op.create_index("ix_shopping_list_items_tag", "shopping_list_items", ["tag"])


def downgrade() -> None:
    op.drop_index("ix_shopping_list_items_tag", table_name="shopping_list_items")
    op.drop_column("shopping_list_items", "tag")
