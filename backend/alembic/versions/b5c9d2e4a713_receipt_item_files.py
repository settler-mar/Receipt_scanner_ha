"""Per-item attachments: photos of a product or of a paper warranty talon.

Revision ID: b5c9d2e4a713
Revises: a4b7c2e91d35
Create Date: 2026-09-21

The manual, non-PDF warranty path: shops that hand out a stamped paper talon
leave nothing downloadable, so the photo taken by hand is the document. Rows
index the files; the bytes live under backend/data/item_files/.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5c9d2e4a713"
down_revision: Union[str, Sequence[str], None] = "a4b7c2e91d35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("receipt_item_files"):
        return
    op.create_table(
        "receipt_item_files",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "item_id",
            sa.String(),
            sa.ForeignKey("receipt_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False, server_default="warranty"),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("original_name", sa.String(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_receipt_item_files_item_id", "receipt_item_files", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_receipt_item_files_item_id", table_name="receipt_item_files")
    op.drop_table("receipt_item_files")
