"""split "Ліки" into sub-categories and re-queue what was filed under it

Revision ID: a3c8e5d7b912
Revises: b6d1a3f5c824
Create Date: 2026-09-21

Pharmacy purchases all landed in the single leaf "Ліки" (see app/seed.py
`CATEGORY_TREE`), which says nothing about what was actually bought. The node
gets seven children here; `seed_categories()` only seeds into an empty table, so
an existing database needs this migration to gain them.

Anything already filed directly under "Ліки" is sent back to the "Без категорії"
queue so the classifier re-splits it into the new children:
  * `product_category` rows pointing at "Ліки" are deleted outright — a row with
    a NULL category is what stops a name being re-queued, so nulling would freeze
    those names instead of re-classifying them;
  * `receipt_items.category_id` / `shopping_list_items.category_id` equal to
    "Ліки" are reset to NULL.

Manual (`source="user"`) rows are dropped too: the user picked "Ліки" when no
finer option existed, and keeping them sticky would defeat the whole re-split.
Items already sitting in a *sibling* category are untouched.

`downgrade` removes the children and folds their items and aliases back onto
the parent.
"""
from __future__ import annotations

from typing import Sequence, Union

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "a3c8e5d7b912"
down_revision: Union[str, Sequence[str], None] = "b6d1a3f5c824"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (name, slug) — slugs mirror app.services.text.slugify for these names, kept
# literal so the migration doesn't depend on app code.
CHILDREN = [
    ("Застуда, грип та ГРВІ", "zastuda-hryp-ta-hrvi"),
    ("Знеболювальні та жарознижувальні", "zneboliuvalni-ta-zharoznyzhuvalni"),
    ("Травлення та печінка", "travlennia-ta-pechinka"),
    ("Антибіотики та протимікробні", "antybiotyky-ta-protymikrobni"),
    ("Серце, судини та тиск", "sertse-sudyny-ta-tysk"),
    ("Алергія та шкіра", "alerhiia-ta-shkira"),
    ("Жіноче та чоловіче здоров'я", "zhinoche-ta-choloviche-zdorovia"),
]


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _parent(bind) -> sa.Row | None:
    return bind.execute(
        sa.text("SELECT id, path FROM categories WHERE path = :path"),
        {"path": "zdorovia-ta-apteka/liky"},
    ).first()


def upgrade() -> None:
    tables = _tables()
    if "categories" not in tables:
        return
    bind = op.get_bind()
    parent = _parent(bind)
    if parent is None:
        # Renamed or removed tree — nothing to split, leave the user's own tree be.
        return
    parent_id, parent_path = parent[0], parent[1]

    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT slug FROM categories WHERE parent_id = :pid"), {"pid": parent_id}
        )
    }
    for order, (name, slug) in enumerate(CHILDREN):
        if slug in existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO categories (id, parent_id, name, slug, path, sort_order)"
                " VALUES (:id, :pid, :name, :slug, :path, :order)"
            ),
            {
                "id": str(uuid.uuid4()),
                "pid": parent_id,
                "name": name,
                "slug": slug,
                "path": f"{parent_path}/{slug}",
                "order": order,
            },
        )

    if "product_category" in tables:
        bind.execute(
            sa.text("DELETE FROM product_category WHERE category_id = :pid"), {"pid": parent_id}
        )
    for table in ("receipt_items", "shopping_list_items"):
        if table in tables:
            bind.execute(
                sa.text(f"UPDATE {table} SET category_id = NULL WHERE category_id = :pid"),
                {"pid": parent_id},
            )


def downgrade() -> None:
    tables = _tables()
    if "categories" not in tables:
        return
    bind = op.get_bind()
    parent = _parent(bind)
    if parent is None:
        return
    parent_id = parent[0]
    child_ids = [
        row[0]
        for row in bind.execute(
            sa.text("SELECT id FROM categories WHERE parent_id = :pid AND slug IN :slugs").bindparams(
                sa.bindparam("slugs", expanding=True)
            ),
            {"pid": parent_id, "slugs": [slug for _, slug in CHILDREN]},
        )
    ]
    if not child_ids:
        return
    params = {"pid": parent_id, "ids": child_ids}
    for table in ("product_category", "receipt_items", "shopping_list_items"):
        if table in tables:
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET category_id = :pid WHERE category_id IN :ids"
                ).bindparams(sa.bindparam("ids", expanding=True)),
                params,
            )
    bind.execute(
        sa.text("DELETE FROM categories WHERE id IN :ids").bindparams(
            sa.bindparam("ids", expanding=True)
        ),
        {"ids": child_ids},
    )
