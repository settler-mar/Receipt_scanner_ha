"""track shopping-list provenance

Revision ID: c6b9c3e5f7a8
Revises: c5a8b2d4e6f7
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "c6b9c3e5f7a8"
down_revision: Union[str, Sequence[str], None] = "c5a8b2d4e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "shopping_list_items" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("shopping_list_items")}
    if "added_method" not in columns:
        op.add_column("shopping_list_items", sa.Column("added_method", sa.String(), nullable=True, server_default="web"))
    if "added_by" not in columns:
        op.add_column("shopping_list_items", sa.Column("added_by", sa.String(), nullable=True))
    op.execute("UPDATE shopping_list_items SET added_method = 'web' WHERE added_method IS NULL")
    op.execute("UPDATE shopping_list_items SET added_by = 'web' WHERE added_by IS NULL AND added_method = 'web'")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "shopping_list_items" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("shopping_list_items")}
        if "added_by" in columns:
            op.drop_column("shopping_list_items", "added_by")
        if "added_method" in columns:
            op.drop_column("shopping_list_items", "added_method")
