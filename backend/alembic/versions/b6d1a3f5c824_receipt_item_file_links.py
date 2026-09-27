"""Item attachments may be a link instead of an uploaded file.

Revision ID: b6d1a3f5c824
Revises: b5c9d2e4a713
Create Date: 2026-09-21

A product photo often already exists somewhere (store's product page, Drive, a
chat) — pointing at it is as good as re-uploading it, so `filename` becomes
optional and `source_url` carries the link instead.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6d1a3f5c824"
down_revision: Union[str, Sequence[str], None] = "b5c9d2e4a713"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("receipt_item_files")}
    with op.batch_alter_table("receipt_item_files") as batch:
        if "source_url" not in columns:
            batch.add_column(sa.Column("source_url", sa.String(), nullable=True))
        # SQLite rewrites the table here, which is also what relaxes NOT NULL.
        batch.alter_column("filename", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("receipt_item_files") as batch:
        batch.drop_column("source_url")
        batch.alter_column("filename", existing_type=sa.String(), nullable=False)
