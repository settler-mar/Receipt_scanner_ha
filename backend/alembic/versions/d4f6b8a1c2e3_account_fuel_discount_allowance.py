"""cache fuel discount allowance on accounts

Revision ID: d4f6b8a1c2e3
Revises: c3e5a1b2d4f6
Create Date: 2026-08-30

The dashboard fuel-price block now reads cached snapshots instead of hitting a
station API on every page load; a background job refreshes them 3×/day and also
caches the monthly discount-allowance state (Ukrnafta) that used to be fetched
live. Adds the three `accounts` columns holding that cached state.

Defensive skip-if-present: `Base.metadata.create_all()` runs before migrations,
so a fresh DB already has these columns. See app/db.py::run_migrations.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f6b8a1c2e3"
down_revision: Union[str, Sequence[str], None] = "c3e5a1b2d4f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


_COLUMNS = {
    "fuel_discount_allowance": sa.Numeric(12, 2),
    "fuel_discount_allowance_unit": sa.String(),
    "fuel_discount_allowance_resets_at": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        for name, type_ in _COLUMNS.items():
            if not _has_column("accounts", name):
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        for name in _COLUMNS:
            if _has_column("accounts", name):
                batch.drop_column(name)
