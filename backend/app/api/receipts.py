from __future__ import annotations

import datetime as dt
import io
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from pypdf import PdfReader
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.orm import Session, selectinload

from app.adapters.base import AdapterError, NotImplementedYet, NormalizedItem, NormalizedReceipt
from app.core.time import store_local_to_utc
from app.adapters.registry import get_adapter
from app.core.config import BASE_DIR
from app.db import get_db
from app.models.models import Account, Receipt, ReceiptItem, ReceiptTag, derive_unit_price
from app.schemas.schemas import (
    ReceiptCreate,
    ExchangeRateBackfillOut,
    ReceiptItemCreate,
    ReceiptItemOut,
    ReceiptItemUpdate,
    ReceiptListOut,
    ReceiptOut,
    ReceiptTagSummary,
    ReceiptUpdate,
    ShopSuggestion,
    TagRename,
)
from app.services.categorize import apply_known_categories, record_user_choice
from app.services.exchange_rates import rate_to_uah
from app.services.exchange_rates import backfill_missing_rates
from app.services.text import normalize_tag, normalize_tags
from app.services.shopping_list import match_receipt
from app.services.collector import ensure_fresh_token, persist_rotated_access_token
from app.services.manual_dedup import find_duplicate, ingestion_lock
from app.services.receipt_visibility import visible_receipt_clause

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

PHOTO_DIR = BASE_DIR / "data" / "photos"
RECEIPT_DIR = BASE_DIR / "data" / "receipts"
_ALLOWED_PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024

WARRANTY_DIR = BASE_DIR / "data" / "warranty"
_MAX_WARRANTY_BYTES = 15 * 1024 * 1024
# Дніпро-М's own phrasing ("Гарантія діє з 09.06.2026 по 09.06.2029") — confirmed
# against a real downloaded warranty-card PDF, see RESEARCH.md. Best-effort only:
# other stores' documents (if ever uploaded here) will just not match, and
# warranty_expires_at stays unset rather than raising — has_warranty/the PDF
# itself are the reliable part, this date is a bonus when the wording lines up.
_WARRANTY_DATE_RANGE_RE = re.compile(
    r"[Гг]арант\S*\s+ді[єе]\s+з\s+(\d{2}\.\d{2}\.\d{4})\s+по\s+(\d{2}\.\d{2}\.\d{4})"
)


def _extract_warranty_expiry(pdf_bytes: bytes) -> dt.datetime | None:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return None
    m = _WARRANTY_DATE_RANGE_RE.search(text)
    if not m:
        return None
    try:
        return store_local_to_utc(dt.datetime.strptime(m.group(2), "%d.%m.%Y"))
    except ValueError:
        return None


def _get_receipt_or_404(receipt_id: str, db: Session) -> Receipt:
    r = db.get(Receipt, receipt_id, options=[
        selectinload(Receipt.items).selectinload(ReceiptItem.category),
        selectinload(Receipt.account),
        selectinload(Receipt.tags),
    ])
    if r is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return r


def _item_data_with_unit_price(item) -> dict:
    data = item.model_dump()
    if data.get("unit_price") is None:
        data["unit_price"] = derive_unit_price(data["quantity"], data["total_price"])
    return data


def _out(r: Receipt) -> ReceiptOut:
    out = ReceiptOut.model_validate(r)
    source_items = {it.id: it for it in r.items}
    for item_out in out.items:
        source_item = source_items[item_out.id]
        item_out.category_path = source_item.category.path if source_item.category else None
        if item_out.unit_price is None:
            item_out.unit_price = derive_unit_price(item_out.quantity, item_out.total_price)
    out.account_label = r.account.label
    out.tags = [t.tag for t in r.tags]
    out.photo_url = f"/api/receipts/{r.id}/photo" if r.photo_filename else None
    receipt_file = (r.raw or {}).get("receipt_file")
    out.receipt_file_url = f"/api/receipts/{r.id}/file" if receipt_file and (RECEIPT_DIR / Path(receipt_file).name).exists() else None
    out.warranty_text = (r.raw or {}).get("warranty_text")
    out.warranty_pdf_url = f"/api/receipts/{r.id}/warranty" if r.warranty_pdf_filename else None
    return out


@router.get("/shops", response_model=list[ShopSuggestion])
def list_shops(db: Session = Depends(get_db)):
    """Distinct (shop_name, shop_address) pairs across *all* receipts — manual
    entry and offline/online alike — so typing "АТБ" once and picking the exact
    branch from the dropdown reuses that same string forever after, instead of
    each manual entry drifting into its own spelling. Must be registered before
    GET /{receipt_id} below or FastAPI would try to treat "shops" as an id."""
    stmt = (
        select(Receipt.shop_name, Receipt.shop_address, func.count().label("n"))
        .where(Receipt.shop_name.isnot(None))
        .group_by(Receipt.shop_name, Receipt.shop_address)
        .order_by(func.count().desc())
        .limit(300)
    )
    rows = db.execute(stmt).all()
    return [ShopSuggestion(shop_name=r.shop_name, shop_address=r.shop_address, count=r.n) for r in rows]


@router.get("", response_model=ReceiptListOut)
def list_receipts(
    db: Session = Depends(get_db),
    store_code: str | None = None,
    account_id: str | None = None,
    channel: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    search: str | None = Query(default=None, description="Free-text over shop name / item names"),
    tag: str | None = Query(default=None, description="Only receipts carrying this hashtag"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    stmt = select(Receipt).options(
        selectinload(Receipt.items).selectinload(ReceiptItem.category),
        selectinload(Receipt.account),
        selectinload(Receipt.tags),
    ).where(visible_receipt_clause())
    if store_code:
        stmt = stmt.where(Receipt.store_code == store_code)
    if account_id:
        stmt = stmt.where(Receipt.account_id == account_id)
    if channel:
        stmt = stmt.where(Receipt.channel == channel)
    if date_from:
        stmt = stmt.where(Receipt.purchased_at >= date_from)
    if date_to:
        stmt = stmt.where(Receipt.purchased_at <= date_to)
    if search:
        stmt = stmt.where(Receipt.shop_name.ilike(f"%{search}%"))
    if tag:
        stmt = stmt.where(
            Receipt.id.in_(select(ReceiptTag.receipt_id).where(ReceiptTag.tag == normalize_tag(tag)))
        )

    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Receipt.purchased_at.desc()).limit(limit).offset(offset)
    ).scalars().all()

    return ReceiptListOut(total=total, items=[_out(r) for r in rows])


# --------------------------------------------------------------------------- #
# Hashtags — group spend across receipts (a trip, a renovation, a gift budget)
# --------------------------------------------------------------------------- #
@router.get("/tags", response_model=list[ReceiptTagSummary])
def list_tags(db: Session = Depends(get_db)):
    """Every hashtag in use, with how many receipts carry it and their total —
    answers "скільки коштувала поїздка до Львова". Sorted by spend, desc."""
    rows = db.execute(
        select(
            ReceiptTag.tag,
            func.count(func.distinct(ReceiptTag.receipt_id)),
            func.coalesce(func.sum(Receipt.total_amount), 0),
        )
        .join(Receipt, Receipt.id == ReceiptTag.receipt_id)
        .group_by(ReceiptTag.tag)
        .order_by(func.sum(Receipt.total_amount).desc())
    ).all()
    return [ReceiptTagSummary(tag=t, receipt_count=c, total_amount=float(s)) for t, c, s in rows]


@router.post("/exchange-rates/backfill", response_model=ExchangeRateBackfillOut)
def backfill_exchange_rates(db: Session = Depends(get_db), limit: int | None = Query(default=None, ge=1, le=5000)):
    return backfill_missing_rates(db, limit=limit)


@router.post("/tags/rename", response_model=list[ReceiptTagSummary])
def rename_tag(payload: TagRename, db: Session = Depends(get_db)):
    old, new = normalize_tag(payload.from_), normalize_tag(payload.to)
    if not old or not new:
        raise HTTPException(status_code=400, detail="Порожня назва тегу")
    # move rows, skipping receipts that already carry the target tag
    already = {r for (r,) in db.execute(
        select(ReceiptTag.receipt_id).where(ReceiptTag.tag == new)
    )}
    for row in db.execute(select(ReceiptTag).where(ReceiptTag.tag == old)).scalars().all():
        if row.receipt_id in already:
            db.delete(row)
        else:
            row.tag = new
    db.commit()
    return list_tags(db)


@router.delete("/tags/{tag}", status_code=204)
def delete_tag(tag: str, db: Session = Depends(get_db)):
    db.execute(sa_delete(ReceiptTag).where(ReceiptTag.tag == normalize_tag(tag)))
    db.commit()


@router.post("", response_model=ReceiptOut, status_code=201)
def create_receipt(payload: ReceiptCreate, db: Session = Depends(get_db)):
    """Manual entry — a receipt from a store this project doesn't automate (or a
    one-off paper receipt, ahead of any OCR scanning), typed in by hand. Doesn't
    go through collector.py at all: no adapter, no CollectionRun, just a direct
    insert under whichever existing Account the user picked."""
    account = db.get(Account, payload.account_id)
    if account is None:
        raise HTTPException(status_code=400, detail="Account not found")

    candidate = NormalizedReceipt(
        external_id="", purchased_at=payload.purchased_at,
        total_amount=payload.total_amount, currency=payload.currency,
        shop_name=payload.shop_name, shop_address=payload.shop_address,
        payment_type=payload.payment_type, channel=payload.channel,
        total_discount=payload.total_discount, receipt_url=payload.receipt_url,
        items=[NormalizedItem(**_item_data_with_unit_price(item)) for item in payload.items],
        raw={"source": "manual", "ingest_scope": "manual"},
    )
    with ingestion_lock():
        duplicate = find_duplicate(db, candidate)
        if duplicate is not None:
            result = _out(_get_receipt_or_404(duplicate.id, db))
            result.duplicate = True
            return result

        receipt = Receipt(
            account_id=account.id,
            store_code=account.store.code,
            external_id=f"manual:{uuid.uuid4()}",
            purchased_at=payload.purchased_at,
            total_amount=payload.total_amount,
            currency=payload.currency,
            exchange_rate_to_uah=payload.exchange_rate_to_uah or rate_to_uah(payload.currency, payload.purchased_at),
            shop_name=payload.shop_name,
            shop_address=payload.shop_address,
            payment_type=payload.payment_type,
            channel=payload.channel,
            total_discount=payload.total_discount,
            receipt_url=payload.receipt_url,
            raw={"source": "manual", "ingest_scope": "manual"},
        )
        db.add(receipt)
        db.flush()
        for tag in normalize_tags(payload.tags):
            db.add(ReceiptTag(receipt_id=receipt.id, tag=tag))
        for it in payload.items:
            db.add(ReceiptItem(receipt_id=receipt.id, **_item_data_with_unit_price(it)))
        db.flush()
        db.refresh(receipt)
        apply_known_categories(db, receipt)
        try:
            match_receipt(db, receipt)  # ROADMAP.md Крок 2 auto-tick — best-effort
        except Exception:  # noqa: BLE001
            pass
        db.commit()
    return _out(_get_receipt_or_404(receipt.id, db))


@router.get("/{receipt_id}", response_model=ReceiptOut)
def get_receipt(receipt_id: str, db: Session = Depends(get_db)):
    return _out(_get_receipt_or_404(receipt_id, db))


@router.get("/{receipt_id}/file")
def get_receipt_file(receipt_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    filename = Path((r.raw or {}).get("receipt_file") or "").name
    path = RECEIPT_DIR / filename
    if not filename or not path.exists():
        raise HTTPException(status_code=404, detail="Локальний файл чека відсутній")
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/html"
    return FileResponse(path, media_type=media_type, filename=path.name)


def _set_tags(r: Receipt, tags: list[str]) -> None:
    """Replace the receipt's tag set with the normalised, de-duplicated `tags`."""
    wanted = set(normalize_tags(tags))
    r.tags[:] = [t for t in r.tags if t.tag in wanted]
    have = {t.tag for t in r.tags}
    for tag in wanted - have:
        r.tags.append(ReceiptTag(tag=tag))


@router.patch("/{receipt_id}", response_model=ReceiptOut)
def update_receipt(receipt_id: str, payload: ReceiptUpdate, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    data = payload.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    for field, value in data.items():
        setattr(r, field, value)
    if "exchange_rate_to_uah" not in data and ("currency" in data or "purchased_at" in data):
        r.exchange_rate_to_uah = rate_to_uah(r.currency, r.purchased_at)
    if tags is not None:
        _set_tags(r, tags)
    db.commit()
    db.refresh(r)
    return _out(_get_receipt_or_404(receipt_id, db))


@router.delete("/{receipt_id}", status_code=204)
def delete_receipt(receipt_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    if r.photo_filename:
        (PHOTO_DIR / r.photo_filename).unlink(missing_ok=True)
    db.delete(r)
    db.commit()


@router.post("/{receipt_id}/items", response_model=ReceiptItemOut, status_code=201)
def add_item(receipt_id: str, payload: ReceiptItemCreate, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    item = ReceiptItem(receipt_id=r.id, **_item_data_with_unit_price(payload))
    db.add(item)
    db.flush()
    db.refresh(r)
    apply_known_categories(db, r)
    db.commit()
    db.refresh(item)
    return item


@router.patch("/{receipt_id}/items/{item_id}", response_model=ReceiptItemOut)
def update_item(receipt_id: str, item_id: str, payload: ReceiptItemUpdate, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    item = next((it for it in r.items if it.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found on this receipt")
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(item, field, value)
    if item.unit_price is None:
        item.unit_price = derive_unit_price(item.quantity, item.total_price)
    # A hand-set category is sticky — remember it against this product name so
    # the background AI pass never overwrites it (categorize.record_user_choice).
    if "category_id" in fields:
        record_user_choice(db, item.name, fields["category_id"])
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{receipt_id}/items/{item_id}", status_code=204)
def delete_item(receipt_id: str, item_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    item = next((it for it in r.items if it.id == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found on this receipt")
    db.delete(item)
    db.commit()


@router.post("/{receipt_id}/photo", response_model=ReceiptOut)
async def upload_photo(receipt_id: str, file: UploadFile, db: Session = Depends(get_db)):
    """One photo per receipt — a fresh upload replaces whatever was there before
    (delete-then-write), simpler than versioning attachments for a personal tool
    with no undo history anywhere else either."""
    r = _get_receipt_or_404(receipt_id, db)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_PHOTO_EXT:
        raise HTTPException(status_code=400, detail=f"Непідтримуваний тип файлу: {ext or '(немає)'}")

    content = await file.read()
    if len(content) > _MAX_PHOTO_BYTES:
        raise HTTPException(status_code=400, detail="Файл завеликий (максимум 15 МБ)")

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    if r.photo_filename:
        (PHOTO_DIR / r.photo_filename).unlink(missing_ok=True)
    filename = f"{r.id}{ext}"
    (PHOTO_DIR / filename).write_bytes(content)

    r.photo_filename = filename
    db.commit()
    return _out(_get_receipt_or_404(receipt_id, db))


@router.get("/{receipt_id}/photo")
def get_photo(receipt_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    if not r.photo_filename:
        raise HTTPException(status_code=404, detail="No photo attached")
    path = PHOTO_DIR / r.photo_filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Photo file missing on disk")
    return FileResponse(path)


@router.delete("/{receipt_id}/photo", response_model=ReceiptOut)
def delete_photo(receipt_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    if r.photo_filename:
        (PHOTO_DIR / r.photo_filename).unlink(missing_ok=True)
        r.photo_filename = None
        db.commit()
    return _out(_get_receipt_or_404(receipt_id, db))


@router.post("/{receipt_id}/warranty", response_model=ReceiptOut)
async def upload_warranty(receipt_id: str, file: UploadFile, db: Session = Depends(get_db)):
    """Manual fallback for stores whose session can't be proxied live from this
    backend (Дніпро-М — see RESEARCH.md): download the warranty PDF yourself,
    logged in in your own browser, and upload it here. Best-effort parses the
    expiry date out of the PDF text (_extract_warranty_expiry) — if the
    wording doesn't match, has_warranty still flips on and the file is still
    kept, just without a parsed date."""
    r = _get_receipt_or_404(receipt_id, db)
    ext = Path(file.filename or "").suffix.lower()
    if ext != ".pdf":
        raise HTTPException(status_code=400, detail="Очікується PDF-файл")

    content = await file.read()
    if len(content) > _MAX_WARRANTY_BYTES:
        raise HTTPException(status_code=400, detail="Файл завеликий (максимум 15 МБ)")

    WARRANTY_DIR.mkdir(parents=True, exist_ok=True)
    if r.warranty_pdf_filename:
        (WARRANTY_DIR / r.warranty_pdf_filename).unlink(missing_ok=True)
    filename = f"{r.id}.pdf"
    (WARRANTY_DIR / filename).write_bytes(content)

    r.warranty_pdf_filename = filename
    r.has_warranty = True
    r.warranty_expires_at = _extract_warranty_expiry(content)
    db.commit()
    return _out(_get_receipt_or_404(receipt_id, db))


@router.get("/{receipt_id}/warranty")
async def get_warranty(receipt_id: str, db: Session = Depends(get_db)):
    """Serves the manually-uploaded PDF when there is one; otherwise falls back
    to proxying the store's own document live (see
    BaseAdapter.get_warranty_document) — not stored on disk in that case,
    since it's fetchable again any time the account's session is actually
    valid and there's no reason to duplicate a file the store already keeps."""
    r = _get_receipt_or_404(receipt_id, db)
    if r.warranty_pdf_filename:
        path = WARRANTY_DIR / r.warranty_pdf_filename
        if path.exists():
            return FileResponse(path, media_type="application/pdf")

    if not r.has_warranty:
        raise HTTPException(status_code=404, detail="Цей чек не має гарантійного документа")
    account: Account = r.account
    if account.credential is None or not account.credential.access_token_enc:
        raise HTTPException(status_code=400, detail="Немає токена — спершу пройдіть авторизацію")
    adapter = get_adapter(account.store.code)
    try:
        access_token = await ensure_fresh_token(db, account, adapter)
        extra = dict(account.credential.extra or {})
        content = await adapter.get_warranty_document(access_token, extra, r.external_id)
        if persist_rotated_access_token(account, extra):
            db.commit()
    except (NotImplementedYet, AdapterError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media_type = "application/pdf" if content.startswith(b"%PDF") else "text/html"
    return Response(content=content, media_type=media_type)


@router.delete("/{receipt_id}/warranty", response_model=ReceiptOut)
def delete_warranty(receipt_id: str, db: Session = Depends(get_db)):
    r = _get_receipt_or_404(receipt_id, db)
    if r.warranty_pdf_filename:
        (WARRANTY_DIR / r.warranty_pdf_filename).unlink(missing_ok=True)
        r.warranty_pdf_filename = None
        r.warranty_expires_at = None
        db.commit()
    return _out(_get_receipt_or_404(receipt_id, db))
