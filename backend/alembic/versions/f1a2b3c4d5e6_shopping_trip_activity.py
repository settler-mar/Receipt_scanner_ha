"""track shopping-trip owner and inactivity timeout

Revision ID: f1a2b3c4d5e6
Revises: d4a7c1e93f6b
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "d4a7c1e93f6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "shopping_trips" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("shopping_trips")}
    if "owner_name" not in columns:
        op.add_column("shopping_trips", sa.Column("owner_name", sa.String(), nullable=True))
    if "last_activity_at" not in columns:
        op.add_column("shopping_trips", sa.Column("last_activity_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE shopping_trips SET owner_name = 'Неизвестно' WHERE owner_name IS NULL")
    op.execute("UPDATE shopping_trips SET last_activity_at = COALESCE(last_activity_at, created_at)")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("shopping_trips")}
    if "last_activity_at" in columns:
        op.drop_column("shopping_trips", "last_activity_at")
    if "owner_name" in columns:
        op.drop_column("shopping_trips", "owner_name")
