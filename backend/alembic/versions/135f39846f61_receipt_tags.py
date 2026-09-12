"""receipt tags

Revision ID: 135f39846f61
Revises: d4f6b8a1c2e3
Create Date: 2026-08-30

Free-form hashtags on receipts (e.g. "поездка_львов_лето_2026") — group spend
across receipts that share neither a store nor a date range.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "135f39846f61"
down_revision: Union[str, Sequence[str], None] = "d4f6b8a1c2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `create_all()` in main.py may have already made this table on an app that
    # restarted before migrations ran — so add it only if it's missing (the two
    # mechanisms still co-exist, see ROADMAP.md § 0.3).
    bind = op.get_bind()
    if sa.inspect(bind).has_table("receipt_tags"):
        return
    op.create_table(
        "receipt_tags",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("receipt_id", sa.String(), sa.ForeignKey("receipts.id"), nullable=False),
        sa.Column("tag", sa.String(), nullable=False),
        sa.UniqueConstraint("receipt_id", "tag", name="uq_receipt_tag"),
    )
    op.create_index("ix_receipt_tags_receipt_id", "receipt_tags", ["receipt_id"])
    op.create_index("ix_receipt_tags_tag", "receipt_tags", ["tag"])


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("receipt_tags"):
        return
    op.drop_index("ix_receipt_tags_tag", table_name="receipt_tags")
    op.drop_index("ix_receipt_tags_receipt_id", table_name="receipt_tags")
    op.drop_table("receipt_tags")
