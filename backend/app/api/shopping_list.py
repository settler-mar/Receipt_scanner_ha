"""Shopping-list endpoints (ROADMAP.md Крок 2).

NOT YET WIRED into app/main.py — that edit belongs with the Крок 1 merge to
avoid a conflict on the router list. To enable:

    from app.api import shopping_list
    app.include_router(shopping_list.router)

and import the models module so the tables register:

    from app.models import shopping  # noqa: F401
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Category, Receipt, ReceiptItem, Store
from app.models.shopping import (
    ShoppingListItem, ShoppingListStatus, ShoppingTrip, ShoppingTripItem,
    ShoppingTripItemStatus, ShoppingTripStatus, ShoppingMatchCandidate,
)
from app.schemas.shopping import (
    LiveSuggestStoreOut,
    RescanResult,
    ShoppingItemCreate,
    ShoppingItemOut,
    ShoppingItemUpdate,
    SuggestStoreOut,
    ShoppingTripCreate, ShoppingTripItemOut, ShoppingTripItemUpdate, ShoppingTripOut,
    ShoppingRoutineAction, ShoppingRoutineCategoryAction, ShoppingRoutineOut,
    ShoppingArchiveReceiptOut,
    ShoppingMatchCandidateOut,
    ShoppingSettingsOut, ShoppingSettingsUpdate,
)
from app.services import shopping_list as svc
from app.services import shopping_settings
from app.services import store_classes
from app.services.text import normalize_name

router = APIRouter(prefix="/api/shopping-list", tags=["shopping-list"])

_ACTIVE = (ShoppingListStatus.pending, ShoppingListStatus.bought)


def _get_or_404(db: Session, item_id: str) -> ShoppingListItem:
    row = db.get(ShoppingListItem, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Пункт списку не знайдено")
    return row


def _store_class(store: Store | None, category: Category | None) -> dict:
    """Where this row will most likely be bought: the chain the user pinned, or
    — when there is none — the class implied by the product category."""
    code = store_classes.class_for_store(store) or store_classes.class_for_category(
        category.path if category else None)
    return {"store_class": code or store_classes.UNKNOWN,
            "store_class_name": store_classes.label(code)}


def _item_out(db: Session, row: ShoppingListItem, *, caches: dict | None = None) -> dict:
    if caches is None:
        category = db.get(Category, row.category_id) if row.category_id else None
        receipt_item = db.get(ReceiptItem, row.bought_receipt_item_id) if row.bought_receipt_item_id else None
        receipt = db.get(Receipt, receipt_item.receipt_id) if receipt_item else None
        store = db.execute(select(Store).where(Store.code == row.target_store_code)).scalar_one_or_none() if row.target_store_code else None
    else:
        category = caches["categories"].get(row.category_id) if row.category_id else None
        receipt_item = caches["receipt_items"].get(row.bought_receipt_item_id) if row.bought_receipt_item_id else None
        receipt = caches["receipts"].get(receipt_item.receipt_id) if receipt_item else None
        store = caches["stores"].get(row.target_store_code) if row.target_store_code else None
    return {**ShoppingItemOut.model_validate(row).model_dump(),
            "category_name": svc.category_name_ru(category) if category else None,
            "category_group": caches["category_groups"].get(category.id) if caches and category else (svc.category_group_name(db, category) if category else None),
            **_store_class(store, category),
            "bought_store_name": receipt.shop_name if receipt and receipt.shop_name else (store.name if store else row.target_store_code),
            "bought_receipt_id": receipt.id if receipt else None,
            "bought_receipt_url": receipt.receipt_url if receipt else None,
            "bought_purchased_at": receipt.purchased_at if receipt else row.bought_at}


@router.get("", response_model=list[ShoppingItemOut])
def list_items(status: str | None = None, db: Session = Depends(get_db)):
    """Default (`status` omitted) returns only items still to buy.
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
        stmt = stmt.where(ShoppingListItem.status == ShoppingListStatus.pending)
    rows = db.execute(stmt.order_by(ShoppingListItem.added_at.desc())).scalars().all()
    category_ids = {row.category_id for row in rows if row.category_id}
    receipt_item_ids = {row.bought_receipt_item_id for row in rows if row.bought_receipt_item_id}
    store_codes = {row.target_store_code for row in rows if row.target_store_code}
    categories = {
        item.id: item for item in db.execute(
            select(Category).where(Category.id.in_(category_ids))
        ).scalars()
    } if category_ids else {}
    receipt_items = {
        item.id: item for item in db.execute(
            select(ReceiptItem).where(ReceiptItem.id.in_(receipt_item_ids))
        ).scalars()
    } if receipt_item_ids else {}
    receipt_ids = {item.receipt_id for item in receipt_items.values()}
    receipts = {
        receipt.id: receipt for receipt in db.execute(
            select(Receipt).where(Receipt.id.in_(receipt_ids))
        ).scalars()
    } if receipt_ids else {}
    stores = {
        store.code: store for store in db.execute(
            select(Store).where(Store.code.in_(store_codes))
        ).scalars()
    } if store_codes else {}
    root_paths = {
        category.path.split("/", 1)[0]
        for category in categories.values()
        if "/" in (category.path or "")
    }
    roots = {
        category.path: category for category in db.execute(
            select(Category).where(Category.path.in_(root_paths))
        ).scalars()
    } if root_paths else {}
    category_groups = {
        category.id: svc.category_name_ru(roots.get(category.path.split("/", 1)[0]))
        for category in categories.values()
        if "/" in (category.path or "") and roots.get(category.path.split("/", 1)[0]) is not None
    }
    caches = {"categories": categories, "receipt_items": receipt_items,
              "receipts": receipts, "stores": stores,
              "category_groups": category_groups}
    result = []
    for row in rows:
        if not row.added_method:
            row.added_method = "web"
        result.append(_item_out(db, row, caches=caches))
    return result


@router.get("/archive", response_model=list[ShoppingArchiveReceiptOut])
def archive(db: Session = Depends(get_db)):
    """Completed shopping-list rows grouped by the receipt that closed them."""
    rows = db.execute(
        select(ShoppingListItem).where(
            ShoppingListItem.status == ShoppingListStatus.bought,
            ShoppingListItem.bought_receipt_item_id.is_not(None),
            ShoppingListItem.auto_matched.is_(False),
        )
        .order_by(ShoppingListItem.bought_at.desc())
    ).scalars().all()
    groups: dict[str | None, dict] = {}
    for row in rows:
        receipt_item = db.get(ReceiptItem, row.bought_receipt_item_id) if row.bought_receipt_item_id else None
        receipt = db.get(Receipt, receipt_item.receipt_id) if receipt_item else None
        key = receipt.id if receipt else None
        if key not in groups:
            groups[key] = {
                "receipt_id": receipt.id if receipt else None,
                "purchased_at": receipt.purchased_at if receipt else row.bought_at,
                "shop_name": receipt.shop_name if receipt else None,
                "store_code": receipt.store_code if receipt else None,
                "total_amount": receipt.total_amount if receipt else None,
                "currency": receipt.currency if receipt else None,
                "receipt_url": receipt.receipt_url if receipt else None,
                "items": [],
            }
        groups[key]["items"].append({
            "shopping_item_id": row.id, "list_name": row.raw_text,
            "qty": row.qty, "unit": row.unit,
            "receipt_item_name": receipt_item.name if receipt_item else None,
            "receipt_item_total": receipt_item.total_price if receipt_item else None,
        })
    def _archive_time(group: dict) -> float:
        value = group["purchased_at"]
        if value is None:
            return 0
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.timestamp()

    return sorted(groups.values(), key=_archive_time, reverse=True)


@router.get("/match-candidates", response_model=list[ShoppingMatchCandidateOut])
def match_candidates(db: Session = Depends(get_db)):
    return svc.pending_match_candidates(db)


@router.get("/settings", response_model=ShoppingSettingsOut)
def get_shopping_settings(db: Session = Depends(get_db)):
    return shopping_settings.load(db)


@router.put("/settings", response_model=ShoppingSettingsOut)
def update_shopping_settings(payload: ShoppingSettingsUpdate, db: Session = Depends(get_db)):
    """Автоклассификация списка: включение и пауза «тишины» после последнего
    добавления. Меняется в Настройках, применяется планировщиком раз в минуту."""
    return shopping_settings.save(db, payload.model_dump(exclude_none=True))


@router.post("/categorize", response_model=dict)
async def categorize_items(db: Session = Depends(get_db)):
    """Categorize pending rows: local dictionaries first, then AI for the rest."""
    return await svc.categorize_pending(db)


@router.post("/match-candidates/{candidate_id}/{action}", response_model=ShoppingItemOut)
def resolve_match_candidate(candidate_id: str, action: str, db: Session = Depends(get_db)):
    candidate = db.get(ShoppingMatchCandidate, candidate_id)
    if candidate is None or action not in {"confirm", "reject"}:
        raise HTTPException(status_code=404, detail="Кандидат не знайдено")
    try:
        result = svc.resolve_match_candidate(db, candidate, accept=action == "confirm")
        return _item_out(db, result)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _trip_payload(db: Session, trip: ShoppingTrip) -> dict:
    items = db.execute(
        select(ShoppingTripItem).where(ShoppingTripItem.trip_id == trip.id)
        .order_by(ShoppingTripItem.created_at.asc())
    ).scalars().all()
    return {"id": trip.id, "store_code": trip.store_code, "owner_name": trip.owner_name,
            "status": trip.status, "created_at": trip.created_at,
            "last_activity_at": trip.last_activity_at,
            "expires_at": trip.last_activity_at + svc._TRIP_IDLE_TIMEOUT,
            "completed_at": trip.completed_at, "items": items}


@router.post("/trips", response_model=ShoppingTripOut, status_code=201)
def create_trip(payload: ShoppingTripCreate, db: Session = Depends(get_db)):
    return _trip_payload(db, svc.create_trip(db, store_code=payload.store_code,
                                             owner_name=payload.owner_name))


@router.get("/trips/active", response_model=list[ShoppingTripOut])
def active_trip(db: Session = Depends(get_db)):
    """Return and synchronize every non-expired active checklist."""
    return [_trip_payload(db, trip) for trip in svc.sync_active_trips(db)]


@router.get("/trips/{trip_id}", response_model=ShoppingTripOut)
def get_trip(trip_id: str, db: Session = Depends(get_db)):
    svc.expire_stale_trips(db)
    trip = db.get(ShoppingTrip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Чеклист не знайдено")
    return _trip_payload(db, trip)


@router.patch("/trips/{trip_id}/items/{trip_item_id}", response_model=ShoppingTripItemOut)
def update_trip_item(trip_id: str, trip_item_id: str, payload: ShoppingTripItemUpdate,
                     db: Session = Depends(get_db)):
    svc.expire_stale_trips(db)
    trip = db.get(ShoppingTrip, trip_id)
    item = db.get(ShoppingTripItem, trip_item_id)
    if trip is None or item is None or item.trip_id != trip.id:
        raise HTTPException(status_code=404, detail="Позицію чеклиста не знайдено")
    if trip.status != ShoppingTripStatus.active:
        raise HTTPException(status_code=409, detail="Чеклист вже завершено")
    return svc.update_trip_item(db, item, ShoppingTripItemStatus(payload.status))


@router.post("/trips/{trip_id}/touch", response_model=ShoppingTripOut)
def touch_trip(trip_id: str, db: Session = Depends(get_db)):
    svc.expire_stale_trips(db)
    trip = db.get(ShoppingTrip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Чеклист не знайдено")
    if trip.status != ShoppingTripStatus.active:
        return _trip_payload(db, trip)
    return _trip_payload(db, svc.touch_trip(db, trip))


@router.post("/trips/{trip_id}/complete", response_model=ShoppingTripOut)
def finish_trip(trip_id: str, db: Session = Depends(get_db)):
    svc.expire_stale_trips(db)
    trip = db.get(ShoppingTrip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Чеклист не знайдено")
    if trip.status != ShoppingTripStatus.active:
        return _trip_payload(db, trip)
    return _trip_payload(db, svc.complete_trip(db, trip))


@router.get("/routines", response_model=list[ShoppingRoutineOut])
def list_routines(db: Session = Depends(get_db)):
    return svc.routine_suggestions(db)


@router.post("/routines/{normalized_name:path}", response_model=ShoppingRoutineOut | None)
def act_on_routine(normalized_name: str, payload: ShoppingRoutineAction,
                   db: Session = Depends(get_db)):
    result = svc.routine_action(db, normalized_name, payload.action,
                                postpone_days=payload.postpone_days)
    return result


@router.post("/routines/category/{category_id}", status_code=204)
def act_on_routine_category(category_id: str, payload: ShoppingRoutineCategoryAction,
                            db: Session = Depends(get_db)):
    if db.get(Category, category_id) is None:
        raise HTTPException(status_code=404, detail="Категорію не знайдено")
    svc.routine_category_action(db, category_id, payload.action)


@router.post("", response_model=ShoppingItemOut, status_code=201)
def create_item_endpoint(payload: ShoppingItemCreate, background: BackgroundTasks,
                         db: Session = Depends(get_db)):
    """HTTP entry point; `create_item` stays callable from Telegram/MCP/capture."""
    return create_item(payload, db=db, background=background)


def create_item(payload: ShoppingItemCreate, db: Session = Depends(get_db),
                background: BackgroundTasks | None = None):
    normalized = normalize_name(payload.raw_text)
    existing = db.execute(
        select(ShoppingListItem)
        .where(
            ShoppingListItem.status == ShoppingListStatus.pending,
            ShoppingListItem.normalized_name == normalized,
        )
        .order_by(ShoppingListItem.added_at.asc())
    ).scalars().first()
    if existing is not None:
        # Adding the same phrase again (including a Telegram retry) is
        # idempotent. Keep one row and enrich it with newly supplied metadata.
        changed = False
        if payload.target_store_code:
            changed = changed or existing.target_store_code != payload.target_store_code
            existing.target_store_code = payload.target_store_code
        if payload.category_id:
            changed = changed or existing.category_id != payload.category_id
            existing.category_id = payload.category_id
        if payload.tag:
            changed = changed or existing.tag != payload.tag
            existing.tag = payload.tag
        if payload.note:
            changed = changed or existing.note != payload.note
            existing.note = payload.note
        if payload.qty and payload.qty > existing.qty:
            changed = True
            existing.qty = payload.qty
        db.commit()
        db.refresh(existing)
        existing._shopping_add_state = "changed" if changed else "existing"
        return existing
    row = ShoppingListItem(
        raw_text=payload.raw_text.strip(),
        normalized_name=normalized,
        qty=payload.qty,
        unit=payload.unit,
        note=payload.note,
        barcode=payload.barcode,
        tag=payload.tag,
        added_method=payload.added_method or "web",
        added_by=payload.added_by or (payload.added_method or "web"),
        target_store_code=payload.target_store_code,
        # Pull a category from the Крок 1 alias cache when the client didn't pin one.
        category_id=payload.category_id or svc.known_category_id(db, payload.raw_text),
    )
    if row.category_id is None:
        svc.categorize_list_item_from_history(db, row)
    db.add(row)
    db.commit()
    db.refresh(row)
    row._shopping_add_state = "new"
    if row.category_id is None and background is not None:
        # The dictionaries know only phrases seen before; ask AI after the
        # response so adding an item stays instant.
        background.add_task(svc.categorize_item_later, row.id)
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
            next_status = ShoppingListStatus(data.pop("status"))
            if next_status == ShoppingListStatus.bought and row.status != ShoppingListStatus.bought:
                row.bought_at = row.bought_at or dt.datetime.now(dt.timezone.utc)
            elif next_status == ShoppingListStatus.pending and row.status == ShoppingListStatus.bought and not row.bought_receipt_item_id:
                row.bought_at = None
            row.status = next_status
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
    return _item_out(db, row)


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
    return _item_out(db, row)


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
    return _item_out(db, row)


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
