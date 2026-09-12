"""archive processed scan jobs

Revision ID: a3c7e5d19b04
Revises: f2e3d4c5b6a7
Create Date: 2026-09-08

The scan journal is append-only, so every photo ever sent keeps showing up in
the inbox long after it was dealt with. `archived_at` lets a finished row be put
away without deleting the record (or the stored image it can still be re-run
from). Null = still in the working list.

Defensive skip-if-present: `Base.metadata.create_all()` runs before migrations,
so a fresh DB already has this column. See app/db.py::run_migrations.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c7e5d19b04"
down_revision: Union[str, Sequence[str], None] = "f2e3d4c5b6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("scan_jobs", "archived_at"):
        with op.batch_alter_table("scan_jobs") as batch:
            batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column("scan_jobs", "archived_at"):
        with op.batch_alter_table("scan_jobs") as batch:
            batch.drop_column("archived_at")
