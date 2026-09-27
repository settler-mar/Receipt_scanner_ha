"""add "Товари для дорослих" under Гігієна та косметика

Revision ID: c9e2f4a7b358
Revises: b7f4d1a9c356
Create Date: 2026-09-21

"Інше" had accumulated a cluster of intimate/adult products (vibrators, anal
toys, BDSM gear) with nowhere better to go. This adds a dedicated leaf under
"Гігієна та косметика" (see app/seed.py `CATEGORY_TREE`) so `seed_categories()`
(which only seeds into an empty table) also grows it on an existing database.

Unlike the "Ліки" migrations (a3c8e5d7b912, b7f4d1a9c356) this one does NOT
move or re-queue any `product_category`/`receipt_items` rows: "Інше" is a
mixed catch-all (also holds plant seedlings, saline, pet food — anything
unclassifiable), and which of its items are actually adult products is a
judgment call a migration can't make reliably by string-matching alone
(`normalize_name()` isn't something migrations reimplement — see other
migrations in this history for that convention). On the one database this
was actually needed for, the move was done by hand through the Categories
page's browse-and-reassign panel (api/categories.py's /items endpoint) —
that same panel is the tool for sorting any stragglers this migration leaves
in "Інше" on another database.

`downgrade` removes the leaf; nothing to unwind for items since none were moved.
"""
from __future__ import annotations

from typing import Sequence, Union

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2f4a7b358"
down_revision: Union[str, Sequence[str], None] = "b7f4d1a9c356"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_CATEGORY = ("Товари для дорослих", "tovary-dlia-doroslykh")
PARENT_PATH = "hihiiena-ta-kosmetyka"


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "categories" not in tables:
        return
    bind = op.get_bind()
    parent = bind.execute(
        sa.text("SELECT id, path FROM categories WHERE path = :path"), {"path": PARENT_PATH}
    ).first()
    if parent is None:
        return
    parent_id, parent_path = parent[0], parent[1]

    name, slug = NEW_CATEGORY
    exists = bind.execute(
        sa.text("SELECT 1 FROM categories WHERE parent_id = :pid AND slug = :slug"),
        {"pid": parent_id, "slug": slug},
    ).first()
    if exists is not None:
        return

    next_order = bind.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) FROM categories WHERE parent_id = :pid"),
        {"pid": parent_id},
    ).scalar_one()
    bind.execute(
        sa.text(
            "INSERT INTO categories (id, parent_id, name, slug, path, sort_order)"
            " VALUES (:id, :pid, :name, :slug, :path, :order)"
        ),
        {"id": str(uuid.uuid4()), "pid": parent_id, "name": name, "slug": slug,
         "path": f"{parent_path}/{slug}", "order": next_order + 1},
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "categories" not in tables:
        return
    bind = op.get_bind()
    _, slug = NEW_CATEGORY
    node = bind.execute(
        sa.text("SELECT id, parent_id FROM categories WHERE slug = :slug"), {"slug": slug}
    ).first()
    if node is None:
        return
    node_id, parent_id = node
    for table in ("product_category", "receipt_items", "shopping_list_items"):
        if table in tables:
            bind.execute(
                sa.text(f"UPDATE {table} SET category_id = :pid WHERE category_id = :cid"),
                {"pid": parent_id, "cid": node_id},
            )
    bind.execute(sa.text("DELETE FROM categories WHERE id = :id"), {"id": node_id})
