"""assign shopping-list rows to an optional store

Revision ID: c8e1f5a7b0c2
Revises: c7d0e4f6a9b1
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "c8e1f5a7b0c2"
down_revision: Union[str, Sequence[str], None] = "c7d0e4f6a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "shopping_list_items" in sa.inspect(bind).get_table_names():
        cols = {c["name"] for c in sa.inspect(bind).get_columns("shopping_list_items")}
        if "target_store_code" not in cols:
            op.add_column("shopping_list_items", sa.Column("target_store_code", sa.String(), nullable=True))
            op.create_index("ix_shopping_list_items_target_store_code", "shopping_list_items", ["target_store_code"])


def downgrade() -> None:
    op.drop_index("ix_shopping_list_items_target_store_code", table_name="shopping_list_items")
    op.drop_column("shopping_list_items", "target_store_code")
