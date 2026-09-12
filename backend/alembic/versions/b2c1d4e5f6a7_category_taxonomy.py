"""category taxonomy + product_category + receipt_items.category_id

Revision ID: b2c1d4e5f6a7
Revises: 7314412a3f75
Create Date: 2026-08-29

ROADMAP.md Крок 1. Adds the nested `categories` tree, the `product_category`
alias cache, `stores.network_type`, and on `receipt_items` renames the raw
store category string to `category_raw` while adding the resolved
`category_id` FK.

Written defensively (skip-if-present): `app/main.py` runs
`Base.metadata.create_all()` *before* `run_migrations()`, so on a fresh DB the
new tables/columns already exist by the time this runs — only an existing
pre-Крок-1 database actually needs the `receipt_items` rename + the two new
`add_column`s. See app/db.py::run_migrations.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c1d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "7314412a3f75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _inspector().get_indexes(table)}


def upgrade() -> None:
    if not _has_table("categories"):
        op.create_table(
            "categories",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("parent_id", sa.String(), nullable=True),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("path", sa.String(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.ForeignKeyConstraint(["parent_id"], ["categories.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("path", name="uq_category_path"),
        )
    if not _has_index("categories", "ix_categories_parent_id"):
        op.create_index("ix_categories_parent_id", "categories", ["parent_id"])
    if not _has_index("categories", "ix_categories_path"):
        op.create_index("ix_categories_path", "categories", ["path"])

    if not _has_table("product_category"):
        op.create_table(
            "product_category",
            sa.Column("normalized_name", sa.String(), nullable=False),
            sa.Column("category_id", sa.String(), nullable=True),
            sa.Column(
                "source",
                sa.Enum("ai", "user", "store", name="productcategorysource"),
                nullable=False,
                server_default="ai",
            ),
            sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("normalized_name"),
        )
    if not _has_index("product_category", "ix_product_category_category_id"):
        op.create_index(
            "ix_product_category_category_id", "product_category", ["category_id"]
        )

    if not _has_column("stores", "network_type"):
        with op.batch_alter_table("stores") as batch:
            batch.add_column(sa.Column("network_type", sa.String(), nullable=True))

    if _has_column("receipt_items", "category") and not _has_column("receipt_items", "category_raw"):
        with op.batch_alter_table("receipt_items") as batch:
            batch.alter_column("category", new_column_name="category_raw")
    if not _has_column("receipt_items", "category_id"):
        with op.batch_alter_table("receipt_items") as batch:
            batch.add_column(sa.Column("category_id", sa.String(), nullable=True))
            batch.create_foreign_key(
                "fk_receipt_items_category_id",
                "categories",
                ["category_id"],
                ["id"],
                ondelete="SET NULL",
            )
    if not _has_index("receipt_items", "ix_receipt_items_category_id"):
        op.create_index("ix_receipt_items_category_id", "receipt_items", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_receipt_items_category_id", table_name="receipt_items")
    with op.batch_alter_table("receipt_items") as batch:
        batch.drop_constraint("fk_receipt_items_category_id", type_="foreignkey")
        batch.drop_column("category_id")
        batch.alter_column("category_raw", new_column_name="category")

    with op.batch_alter_table("stores") as batch:
        batch.drop_column("network_type")

    op.drop_index("ix_product_category_category_id", table_name="product_category")
    op.drop_table("product_category")
    op.drop_index("ix_categories_path", table_name="categories")
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_table("categories")
    sa.Enum(name="productcategorysource").drop(op.get_bind(), checkfirst=True)
