"""Shopping-list endpoints (ROADMAP.md Крок 2).

NOT YET WIRED into app/main.py — that edit belongs with the Крок 1 merge to
avoid a conflict on the router list. To enable:

    from app.api import shopping_list
    app.include_router(shopping_list.router)

and import the models module so the tables register:

    from app.models import shopping  # noqa: F401
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.shopping import ShoppingListItem, ShoppingListStatus
from app.schemas.shopping import (
    LiveSuggestStoreOut,
    RescanResult,
    ShoppingItemCreate,
    ShoppingItemOut,
    ShoppingItemUpdate,
    SuggestStoreOut,
)
from app.services import shopping_list as svc
from app.services.text import normalize_name

router = APIRouter(prefix="/api/shopping-list", tags=["shopping-list"])

_ACTIVE = (ShoppingListStatus.pending, ShoppingListStatus.bought)


def _get_or_404(db: Session, item_id: str) -> ShoppingListItem:
    row = db.get(ShoppingListItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Пункт списку не знайдено")
    return row


@router.get("", response_model=list[ShoppingItemOut])
def list_items(status: str | None = None, db: Session = Depends(get_db)):
    """Default (`status` omitted) returns the active list — pending + bought.
    Pass `status=all` for everything, or a specific status value."""
    stmt = select(ShoppingListItem)
    if status == "all":
        pass
    elif status:
        try:
            stmt = stmt.where(ShoppingListItem.status == ShoppingListStatus(status))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"невідомий статус {status!r}") from exc
    else:
        stmt = stmt.where(ShoppingListItem.status.in_(_ACTIVE))
    rows = db.execute(stmt.order_by(ShoppingListItem.added_at.desc())).scalars().all()
    return rows


@router.post("", response_model=ShoppingItemOut, status_code=201)
def create_item(payload: ShoppingItemCreate, db: Session = Depends(get_db)):
    row = ShoppingListItem(
        raw_text=payload.raw_text.strip(),
        normalized_name=normalize_name(payload.raw_text),
        qty=payload.qty,
        unit=payload.unit,
        note=payload.note,
        barcode=payload.barcode,
        # Pull a category from the Крок 1 alias cache when the client didn't pin one.
        category_id=payload.category_id or svc.known_category_id(db, payload.raw_text),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    # A freshly added item may already have been bought on a recent receipt.
    for m in svc.rescan_pending(db):
        if m.id == row.id:
            db.refresh(row)
            break
    return row


@router.patch("/{item_id}", response_model=ShoppingItemOut)
def update_item(item_id: str, payload: ShoppingItemUpdate, db: Session = Depends(get_db)):
    row = _get_or_404(db, item_id)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data:
        try:
            row.status = ShoppingListStatus(data.pop("status"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="невідомий статус") from exc
    if "raw_text" in data:
        row.raw_text = data["raw_text"].strip()
        row.normalized_name = normalize_name(data["raw_text"])
        if "category_id" not in data and row.category_id is None:
            row.category_id = svc.known_category_id(db, data["raw_text"])
        data.pop("raw_text")
    for field, value in data.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: str, db: Session = Depends(get_db)):
    row = _get_or_404(db, item_id)
    db.delete(row)
    db.commit()


@router.post("/{item_id}/confirm", response_model=ShoppingItemOut)
def confirm_item(item_id: str, db: Session = Depends(get_db)):
    """Accept an auto-match candidate: it stays `bought`, no longer flagged."""
    row = _get_or_404(db, item_id)
    row.auto_matched = False
    if row.status != ShoppingListStatus.bought:
        row.status = ShoppingListStatus.bought
    db.commit()
    db.refresh(row)
    return row


@router.post("/{item_id}/reject", response_model=ShoppingItemOut)
def reject_item(item_id: str, db: Session = Depends(get_db)):
    """Reject an auto-match: back to pending, and remember which receipt line was
    wrong so the next collection pass doesn't re-propose it."""
    row = _get_or_404(db, item_id)
    row.rejected_receipt_item_id = row.bought_receipt_item_id
    row.status = ShoppingListStatus.pending
    row.auto_matched = False
    row.bought_receipt_item_id = None
    row.bought_at = None
    db.commit()
    db.refresh(row)
    return row


@router.get("/suggest-store", response_model=list[SuggestStoreOut])
def suggest_store(db: Session = Depends(get_db)):
    return svc.suggest_stores(db)


@router.post("/suggest-store/live", response_model=list[LiveSuggestStoreOut])
async def suggest_store_live(db: Session = Depends(get_db)):
    """Price the pending list against each store's *live* catalogue. Resolves
    every phrase to a store-specific query via AI (`ai.normalize_query`, cached
    in `product_aliases`); without an AI backend it searches on the raw phrase.
    POST, not GET — it spends AI tokens and hits external APIs."""
    return await svc.suggest_stores_live(db)


@router.post("/rescan", response_model=RescanResult)
def rescan(db: Session = Depends(get_db)):
    return RescanResult(matched=svc.rescan_pending(db))
