"""Category-tree CRUD (ROADMAP.md Крок 1).

The materialised `path` column ("produkty/molochne") is maintained here — every
create / rename / move recomputes it for the affected node and its whole
subtree, so the by-category dashboard rollup stays a plain `LIKE 'path/%'`.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Category, ProductCategory, ReceiptItem
from app.schemas.schemas import (
    BackfillResult,
    CategoryCreate,
    CategoryOut,
    CategoryStat,
    CategoryStatsOut,
    CategoryUpdate,
)
from app.services import ai
from app.services.categorize import (
    apply_guesses,
    backfill_all,
    parse_classification_result,
    pending_names,
    reset_all_classifications,
    taxonomy_paths,
)
from app.services.text import slugify

router = APIRouter(prefix="/api/categories", tags=["categories"])


def _get_or_404(db: Session, category_id: str) -> Category:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категорію не знайдено")
    return cat


def _unique_path(db: Session, base: str, exclude_id: str | None = None) -> str:
    """Append -2, -3… to the last segment until `path` is free."""
    candidate = base
    n = 1
    while True:
        row = db.execute(
            select(Category.id).where(Category.path == candidate)
        ).scalar_one_or_none()
        if row is None or row == exclude_id:
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def _child_path(db: Session, parent: Category | None, slug: str, exclude_id: str | None = None) -> str:
    base = f"{parent.path}/{slug}" if parent is not None else slug
    return _unique_path(db, base, exclude_id)


def _descendants(db: Session, cat: Category) -> list[Category]:
    return list(
        db.execute(
            select(Category).where(Category.path.like(f"{cat.path}/%"))
        ).scalars().all()
    )


def _reslug_subtree(db: Session, cat: Category, new_path: str) -> None:
    """Move `cat` to `new_path` and shift every descendant's path prefix."""
    old_prefix = cat.path + "/"
    for child in _descendants(db, cat):
        child.path = new_path + "/" + child.path[len(old_prefix):]
    cat.path = new_path


@router.get("", response_model=list[CategoryOut])
def list_categories(db: Session = Depends(get_db)):
    return db.execute(select(Category).order_by(Category.path)).scalars().all()


@router.get("/stats", response_model=CategoryStatsOut)
def category_stats(db: Session = Depends(get_db)):
    """Per-node item counts for the Categories page: `direct_items` is tagged
    exactly that node, `rollup_items` folds in all descendants. Registered
    before any `/{category_id}` route so "stats" isn't taken for an id."""
    rows = db.execute(
        select(ReceiptItem.category_id, func.count().label("n"))
        .group_by(ReceiptItem.category_id)
    ).all()
    direct = {r.category_id: r.n for r in rows}

    cats = db.execute(select(Category).order_by(Category.path)).scalars().all()
    per: list[CategoryStat] = []
    for cat in cats:
        d = direct.get(cat.id, 0)
        roll = d + sum(
            direct.get(other.id, 0)
            for other in cats
            if other.path.startswith(cat.path + "/")
        )
        per.append(CategoryStat(category_id=cat.id, direct_items=d, rollup_items=roll))

    classified_names = db.execute(
        select(func.count())
        .select_from(ProductCategory)
        .where(ProductCategory.category_id.is_not(None))
    ).scalar_one()

    return CategoryStatsOut(
        total_items=sum(n for cid, n in direct.items() if cid is not None),
        uncategorized_items=direct.get(None, 0),
        classified_names=classified_names,
        per_category=per,
    )


@router.post("", response_model=CategoryOut, status_code=201)
def create_category(payload: CategoryCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Порожня назва")
    parent = _get_or_404(db, payload.parent_id) if payload.parent_id else None
    slug = slugify(name)
    next_order = db.execute(
        select(func.coalesce(func.max(Category.sort_order), -1) + 1).where(
            Category.parent_id == (parent.id if parent else None)
        )
    ).scalar_one()
    cat = Category(
        parent_id=parent.id if parent else None,
        name=name,
        slug=slug,
        path=_child_path(db, parent, slug),
        sort_order=next_order,
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.patch("/{category_id}", response_model=CategoryOut)
def update_category(category_id: str, payload: CategoryUpdate, db: Session = Depends(get_db)):
    cat = _get_or_404(db, category_id)
    fields = payload.model_dump(exclude_unset=True)

    if "sort_order" in fields and fields["sort_order"] is not None:
        cat.sort_order = fields["sort_order"]

    new_parent = cat.parent
    reparenting = "parent_id" in fields and fields["parent_id"] != cat.parent_id
    if reparenting:
        if fields["parent_id"] is None:
            new_parent = None
        else:
            new_parent = _get_or_404(db, fields["parent_id"])
            if new_parent.id == cat.id or new_parent.path.startswith(cat.path + "/"):
                raise HTTPException(status_code=400, detail="Не можна вкласти категорію в себе")

    renaming = "name" in fields and fields["name"] and fields["name"].strip() != cat.name
    if renaming:
        cat.name = fields["name"].strip()
        cat.slug = slugify(cat.name)

    if reparenting or renaming:
        cat.parent = new_parent  # keeps both sides of the collection in sync
        new_path = _child_path(db, new_parent, cat.slug, exclude_id=cat.id)
        _reslug_subtree(db, cat, new_path)

    db.commit()
    db.refresh(cat)
    return cat


@router.delete("/{category_id}", status_code=204)
def delete_category(category_id: str, db: Session = Depends(get_db)):
    cat = _get_or_404(db, category_id)

    # Promote direct children to the deleted node's parent, then fix their paths.
    new_parent = cat.parent
    for child in list(cat.children):
        child.parent = new_parent  # detaches from cat.children too
        new_path = _child_path(db, new_parent, child.slug, exclude_id=child.id)
        _reslug_subtree(db, child, new_path)

    db.execute(
        update(ReceiptItem).where(ReceiptItem.category_id == cat.id).values(category_id=None)
    )
    db.execute(delete(ProductCategory).where(ProductCategory.category_id == cat.id))
    db.delete(cat)
    db.commit()


@router.post("/backfill", response_model=BackfillResult)
async def backfill_categories(
    force: bool = Query(
        default=False,
        description="Wipe all AI classifications (keeping manual ones) and re-split the whole history",
    ),
    db: Session = Depends(get_db),
):
    """Classify product names in history via the configured AI backend
    (ROADMAP.md "Разовий бекфіл усієї історії").

    * default — only the "Без категорії" queue (unknown names);
    * `force=true` — drop every AI-made category first, then re-classify
      everything from scratch (manual choices stay).

    Returns `{names_classified: 0}` when no AI backend is set up."""
    return await backfill_all(db, force=force)


# --------------------------------------------------------------------------- #
# Manual classification — "bring your own AI" (ROADMAP.md Крок 1)
# --------------------------------------------------------------------------- #
class ManualBatch(BaseModel):
    index: int             # 1-based
    count: int
    filename: str
    prompt: str            # this batch's numbered product list (paste after `preamble`)


class ManualPromptOut(BaseModel):
    count: int             # total names in this run
    names: list[str]       # ordered — the answer's numeric ids index into this
    system: str
    prompt: str            # single combined prompt for ALL names (one-message mode)
    filename: str
    preamble: str          # shared instruction block for the batched flow
    batches: list[ManualBatch]   # per-file numbered product lists (files mode)


class ManualApplyIn(BaseModel):
    result: str                       # whatever the user pasted back
    names: list[str] | None = None    # the `names` list from /manual-prompt


@router.get("/manual-prompt", response_model=ManualPromptOut)
def manual_prompt(
    limit: int = Query(default=40, ge=1, le=5000),
    batch_size: int = Query(
        default=40, ge=1, le=300,
        description="Chunk size for the per-file `batches` split",
    ),
    db: Session = Depends(get_db),
):
    """Build a ready-to-paste classification prompt for the next `limit` unknown
    product names. No API key needed — the user runs it in any chatbot and pastes
    the JSON answer into POST /manual-apply.

    Two shapes are returned for the two UI modes:
      * `prompt` — one message with every name (paste into a single chat turn);
      * `preamble` + `batches` — for the batched flow: paste `preamble` once
        (instructions + taxonomy, no products), then feed each batch file (a bare
        product list) as the next message.
    """
    taxonomy = taxonomy_paths(db)
    if not taxonomy:
        raise HTTPException(status_code=400, detail="Дерево категорій порожнє")
    names = pending_names(db, limit)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M")

    batches: list[ManualBatch] = []
    start = 1
    chunks = [names[i:i + batch_size] for i in range(0, len(names), batch_size)] or [[]]
    for idx, chunk in enumerate(chunks, start=1):
        batches.append(ManualBatch(
            index=idx, count=len(chunk),
            filename=f"category-classify-{ts}-{idx:02d}.txt",
            prompt=ai.build_classify_batch(chunk, start=start),
        ))
        start += len(chunk)

    return ManualPromptOut(
        count=len(names), names=names, system=ai._JSON_SYSTEM,
        prompt=ai.build_manual_prompt(names, taxonomy),
        filename=f"category-classify-{ts}.txt",
        preamble=ai.build_classify_preamble(taxonomy),
        batches=batches,
    )


@router.post("/reset")
def reset_classifications(db: Session = Depends(get_db)):
    """Wipe **all** categorisation — AI and manual aliases plus every item
    category. No AI backend needed. The whole history returns to the
    "Без категорії" queue."""
    return reset_all_classifications(db)


@router.post("/manual-apply")
def manual_apply(payload: ManualApplyIn, db: Session = Depends(get_db)):
    """Apply a classification answer produced outside the app. Same effect as the
    automatic pass: caches names in product_category and stamps matching items;
    sticky manual choices are untouched."""
    try:
        guesses = parse_classification_result(payload.result, payload.names)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Не вдалося розібрати відповідь: {exc}") from exc
    if not guesses:
        raise HTTPException(status_code=400, detail="У відповіді немає жодного товару")
    return apply_guesses(db, dict(guesses), backend="manual")
