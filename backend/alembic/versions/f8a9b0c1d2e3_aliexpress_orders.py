"""AliExpress order status, tracking and product metadata.

Revision ID: f8a9b0c1d2e3
Revises: e7a2c4b91f30
"""
from alembic import op
import sqlalchemy as sa

revision = "f8a9b0c1d2e3"
down_revision = "e7a2c4b91f30"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    receipt_columns = {c["name"] for c in inspector.get_columns("receipts")}
    item_columns = {c["name"] for c in inspector.get_columns("receipt_items")}
    if "order_status" not in receipt_columns:
        op.add_column("receipts", sa.Column("order_status", sa.String(), nullable=True))
    if "tracking_numbers" not in receipt_columns:
        op.add_column("receipts", sa.Column("tracking_numbers", sa.JSON(), nullable=False, server_default="[]"))
    if "merchant_name" not in item_columns:
        op.add_column("receipt_items", sa.Column("merchant_name", sa.String(), nullable=True))
    if "product_image_urls" not in item_columns:
        op.add_column("receipt_items", sa.Column("product_image_urls", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("receipt_items", "product_image_urls")
    op.drop_column("receipt_items", "merchant_name")
    op.drop_column("receipts", "tracking_numbers")
    op.drop_column("receipts", "order_status")
