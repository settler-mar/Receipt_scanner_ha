"""rebuild category taxonomy — wipe old tree + all classifications

Revision ID: e7a2c4b91f30
Revises: 135f39846f61
Create Date: 2026-08-30

The category tree was reviewed and rebuilt from scratch (app/seed.py
`CATEGORY_TREE`, "Класифікація покупок та витрат"). `seed_categories()` only
seeds into an empty table, so on an existing database this migration clears the
old tree — and every classification that pointed at it — so the new tree
re-seeds clean on the next startup (main.py lifespan runs migrations, then
`seed_categories()`).

Wiped:
  * `product_category` — the whole alias cache (AI *and* manual `source="user"`
    rows: the old category ids are gone);
  * `receipt_items.category_id` / `shopping_list_items.category_id` — reset to NULL;
  * `categories` — the old nodes.

After this the "Без категорії" queue holds the entire history again; run the
backfill (Категорії → «Розібрати "Без категорії"») or the manual flow to
re-classify against the new tree.

Not reversible — `downgrade` only drops the (new) tree the same way; the
previous tree is defined solely by the old seed and is not restored.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "e7a2c4b91f30"
down_revision: Union[str, Sequence[str], None] = "135f39846f61"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("product_category", "shopping_list_items", "receipt_items", "categories")


def _existing() -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(op.get_bind()).get_table_names())


def _wipe() -> None:
    tables = _existing()
    if "product_category" in tables:
        op.execute("DELETE FROM product_category")
    if "receipt_items" in tables:
        op.execute("UPDATE receipt_items SET category_id = NULL")
    if "shopping_list_items" in tables:
        op.execute("UPDATE shopping_list_items SET category_id = NULL")
    if "categories" in tables:
        # self-referential parent_id — clear it first so the row deletes don't
        # depend on ordering even if FK enforcement is ever turned on.
        op.execute("UPDATE categories SET parent_id = NULL")
        op.execute("DELETE FROM categories")


def upgrade() -> None:
    _wipe()


def downgrade() -> None:
    _wipe()
