"""grow "Ліки" from 7 to 15 sub-categories, re-queue everything already split

Revision ID: b7f4d1a9c356
Revises: a3c8e5d7b912
Create Date: 2026-09-21

`a3c8e5d7b912` gave "Ліки" 7 children and re-queued what was filed directly on
the parent. That first split is now finer-grained: 8 more children (see
app/seed.py `CATEGORY_TREE`), covering respiratory/ENT/eye complaints, the
nervous system, joints, kidneys/urology and endocrinology, which used to have
nowhere to go but a generic sibling.

Unlike the previous migration this one re-queues the *whole* branch, not just
the parent: every item/alias already sorted into one of the 7 original children
is sent back to "Без категорії" too, so the AI re-splits across all 15 with the
finer options in view — the user asked for a full re-split, not just coverage
for the new 8. `product_category` rows are deleted outright (not nulled — a
NULL-category row is what stops a name being re-queued); `receipt_items` /
`shopping_list_items` are reset to NULL. Manual (`source="user"`) picks are
dropped too, same rationale as `a3c8e5d7b912`.

`downgrade` removes the 8 new leaves and folds their items/aliases onto the
parent "Ліки" (matching what `a3c8e5d7b912`'s downgrade would restore).
"""
from __future__ import annotations

from typing import Sequence, Union

import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "b7f4d1a9c356"
down_revision: Union[str, Sequence[str], None] = "a3c8e5d7b912"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_CHILDREN = [
    ("Противірусні та імуностимулятори", "protyvirusni-ta-imunostymuliatory"),
    ("Кашель та дихальні шляхи", "kashel-ta-dykhalni-shliakhy"),
    ("Ніс, горло та вуха", "nis-horlo-ta-vukha"),
    ("Очі", "ochi"),
    ("Нервова система та сон", "nervova-systema-ta-son"),
    ("Суглоби, м'язи та спина", "suhloby-miazy-ta-spyna"),
    ("Нирки та сечостатева система", "nyrky-ta-sechostateva-systema"),
    ("Ендокринологія та цукровий діабет", "endokrynolohiia-ta-tsukrovyi-diabet"),
]

# The 7 children a3c8e5d7b912 created — re-queued here too (see docstring).
OLD_CHILDREN_SLUGS = [
    "zastuda-hryp-ta-hrvi",
    "zneboliuvalni-ta-zharoznyzhuvalni",
    "travlennia-ta-pechinka",
    "antybiotyky-ta-protymikrobni",
    "sertse-sudyny-ta-tysk",
    "alerhiia-ta-shkira",
    "zhinoche-ta-choloviche-zdorovia",
]


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _parent(bind) -> sa.Row | None:
    return bind.execute(
        sa.text("SELECT id, path FROM categories WHERE path = :path"),
        {"path": "zdorovia-ta-apteka/liky"},
    ).first()


def _child_ids(bind, parent_id: str, slugs: list[str]) -> list[str]:
    if not slugs:
        return []
    rows = bind.execute(
        sa.text("SELECT id FROM categories WHERE parent_id = :pid AND slug IN :slugs").bindparams(
            sa.bindparam("slugs", expanding=True)
        ),
        {"pid": parent_id, "slugs": slugs},
    )
    return [row[0] for row in rows]


def upgrade() -> None:
    tables = _tables()
    if "categories" not in tables:
        return
    bind = op.get_bind()
    parent = _parent(bind)
    if parent is None:
        return
    parent_id, parent_path = parent[0], parent[1]

    existing_slugs = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT slug FROM categories WHERE parent_id = :pid"), {"pid": parent_id}
        )
    }
    next_order = bind.execute(
        sa.text("SELECT COALESCE(MAX(sort_order), -1) FROM categories WHERE parent_id = :pid"),
        {"pid": parent_id},
    ).scalar_one()
    for offset, (name, slug) in enumerate(NEW_CHILDREN, start=1):
        if slug in existing_slugs:
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
                "order": next_order + offset,
            },
        )

    # Re-queue the whole branch: the parent itself plus every one of the 7
    # original children (b7f4d1a9c356's own new leaves start empty already).
    requeue_ids = [parent_id] + _child_ids(bind, parent_id, OLD_CHILDREN_SLUGS)
    if "product_category" in tables:
        bind.execute(
            sa.text("DELETE FROM product_category WHERE category_id IN :ids").bindparams(
                sa.bindparam("ids", expanding=True)
            ),
            {"ids": requeue_ids},
        )
    for table in ("receipt_items", "shopping_list_items"):
        if table in tables:
            bind.execute(
                sa.text(f"UPDATE {table} SET category_id = NULL WHERE category_id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": requeue_ids},
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
    child_ids = _child_ids(bind, parent_id, [slug for _, slug in NEW_CHILDREN])
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
