"""Upload routing + document inbox + scan journal (ROADMAP.md Крок 3).

`POST /api/inbox/upload` is the single entry point: it measures the image, and
either runs the receipt-recognition pipeline (app.scanner) or files the image as
a personal document (app.services.inbox) — the two branches never share code and
only the receipt branch may reach app.services.ai.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import oauth_state
from app.core.config import BASE_DIR
from app.core.time import as_utc
from app.services import drive as drive_svc
from app.services import tax_cabinet_settings as tax_settings

from app.adapters.base import NormalizedReceipt
from app.db import get_db
from app.models.models import (
    Account,
    AccountStatus,
    Receipt,
    ReceiptItem,
    Store,
)
from app.models.scanner import (
    InboxDocument,
    InboxStatus,
    ScanJob,
    ScanSample,
    ScanStatus,
)
from app.scanner.aspect import color_stats, image_dimensions, route
from app.scanner.pipeline import AiReading, ScanOutcome, scan_receipt_bytes
from app.scanner.reconcile import reconcile
from app.scanner import seller_lookup
from app.services import inbox as inbox_svc
from app.services import scan_jobs
from app.services.categorize import apply_known_categories
from app.services.shopping_list import match_receipt
from app.services.manual_dedup import find_duplicate, find_receipt_by_fiscal, ingestion_lock
from app.services.exchange_rates import rate_to_uah
from app.services.reconciliation import recalculate_receipt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

_MAX_BYTES_HARD = 40 * 1024 * 1024

# `parents[1]` used to land inside the package (app/app/data) rather than in the
# data directory the deployment mounts — every stored frame vanished with the
# next container rebuild, and the journal's thumbnails 404'd. Anything the app
# keeps belongs under BASE_DIR/data, like photos and voice notes do.
SCAN_INPUT_DIR = BASE_DIR / "data" / "scan_inputs"
_LEGACY_SCAN_INPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "scan_inputs"


def migrate_legacy_scan_inputs() -> int:
    """Move frames left in the old in-package folder to the data directory."""
    if not _LEGACY_SCAN_INPUT_DIR.is_dir() or _LEGACY_SCAN_INPUT_DIR == SCAN_INPUT_DIR:
        return 0
    SCAN_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in _LEGACY_SCAN_INPUT_DIR.iterdir():
        if not path.is_file():
            continue
        target = SCAN_INPUT_DIR / path.name
        if target.exists():
            continue
        try:
            shutil.move(str(path), target)
            moved += 1
        except OSError:
            logger.warning("could not move scan input %s", path.name, exc_info=True)
    if moved:
        logger.info("moved %s stored scan inputs into %s", moved, SCAN_INPUT_DIR)
    return moved


def scan_input_path(job_id: str) -> Path:
    """Where the original uploaded bytes for a scan job are kept on disk."""
    return SCAN_INPUT_DIR / job_id


_MEDIA_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
              "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif"}


def attach_scan_photo(db: Session, receipt: Receipt, data: bytes, media_type: str) -> None:
    """Keep the scanned image as the receipt's photo.

    A receipt recognised from a photo should show that photo like a manually
    added one does — the scan-input copy is an internal debugging artefact and
    isn't reachable from the receipt page. Never overwrites an existing photo.
    """
    if receipt.photo_filename:
        return
    from app.api.receipts import PHOTO_DIR

    ext = _MEDIA_EXT.get((media_type or "").split(";", 1)[0].strip().lower(), ".jpg")
    try:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / f"{receipt.id}{ext}").write_bytes(data)
    except OSError:  # noqa: BLE001 — the parsed receipt matters more than its image
        logger.warning("could not store scan photo for receipt %s", receipt.id, exc_info=True)
        return
    receipt.photo_filename = f"{receipt.id}{ext}"


# --------------------------------------------------------------------------- #
# Scanner account + receipt persistence
# --------------------------------------------------------------------------- #
def _scanner_account(db: Session) -> Account:
    acc = db.execute(
        select(Account).join(Store).where(Store.code == "scanner")
    ).scalars().first()
    if acc is not None:
        return acc
    store = db.execute(select(Store).where(Store.code == "scanner")).scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=500, detail="Store 'scanner' not seeded")
    acc = Account(store_id=store.id, label="Сканер чеків", status=AccountStatus.active)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def _merge_normalized(db: Session, existing: Receipt, nr: NormalizedReceipt) -> bool:
    changed = False
    exchange_rate = nr.exchange_rate_to_uah
    if exchange_rate is None:
        exchange_rate = rate_to_uah(nr.currency, nr.purchased_at)
    if nr.store_code and existing.store_code in ("scanner",) and nr.store_code != existing.store_code:
        # The purchase was filed under the ingest channel until the network
        # behind the till became known.
        existing.store_code = nr.store_code
        changed = True
    for attr in ("shop_name", "shop_address", "payment_type", "receipt_url"):
        value = getattr(nr, attr)
        if not getattr(existing, attr) and value:
            setattr(existing, attr, value)
            changed = True
    if existing.currency != nr.currency:
        existing.currency = nr.currency
        changed = True
    if (nr.currency or "UAH").upper() == "UAH" and existing.exchange_rate_to_uah is not None:
        existing.exchange_rate_to_uah = None
        changed = True
    if exchange_rate is not None and existing.exchange_rate_to_uah != exchange_rate:
        existing.exchange_rate_to_uah = exchange_rate
        changed = True
    incoming_raw = nr.raw or {}
    merged_raw = {**(existing.raw or {}), **incoming_raw}
    if merged_raw != (existing.raw or {}):
        existing.raw = merged_raw
        changed = True
    if reconcile(nr).ok and len(nr.items) > len(existing.items):
        existing.purchased_at = nr.purchased_at
        existing.total_amount = nr.total_amount
        existing.currency = nr.currency
        existing.exchange_rate_to_uah = exchange_rate
        existing.total_discount = (
            nr.total_discount if nr.total_discount is not None
            else sum(item.discount for item in nr.items)
        )
        existing.items.clear()
        for item in nr.items:
            existing.items.append(ReceiptItem(
                name=item.name, barcode=item.barcode, product_code=item.product_code or item.barcode, category_raw=item.category,
                quantity=item.quantity, unit=item.unit, unit_price=item.unit_price,
                total_price=item.total_price, discount=item.discount,
            ))
        apply_known_categories(db, existing)
        changed = True
    return changed


def _persist_receipt_unlocked(db: Session, account: Account, nr: NormalizedReceipt) -> Receipt:
    """Insert a NormalizedReceipt under `account` (dedup by external_id), then run
    category tagging + shopping-list auto-tick. `store_code` comes from the
    account's own store — the scanner account resolves to "scanner", a real store
    account (e.g. a manual fuel purchase via the MCP server) to that store."""
    if account.store.code == "scanner":
        nr.raw = {**(nr.raw or {}), "ingest_scope": "manual"}
    # A ДПС document names no shop: recover it from the same till seen before.
    seller_lookup.enrich(db, nr)
    existing = db.execute(
        select(Receipt).where(
            Receipt.account_id == account.id, Receipt.external_id == nr.external_id
        )
    ).scalar_one_or_none()
    if existing is None and account.store.code == "scanner":
        existing = find_duplicate(db, nr)

    if existing is not None:
        # A first scan may contain a collapsed bank-app preview while a later
        # scan of the full ДПС receipt has every line. Keep the stable receipt
        # id, but upgrade missing metadata and replace items only when the new
        # version reconciles and is demonstrably more complete.
        changed = _merge_normalized(db, existing, nr)
        if changed:
            db.flush()
            recalculate_receipt(db, existing)
            db.commit()
            db.refresh(existing)
        return existing

    exchange_rate = nr.exchange_rate_to_uah
    if exchange_rate is None:
        exchange_rate = rate_to_uah(nr.currency, nr.purchased_at)
    receipt = Receipt(
        account_id=account.id,
        store_code=nr.store_code or account.store.code,
        external_id=nr.external_id,
        purchased_at=nr.purchased_at,
        total_amount=nr.total_amount,
        currency=nr.currency,
        exchange_rate_to_uah=exchange_rate,
        shop_name=nr.shop_name,
        shop_address=nr.shop_address,
        payment_type=nr.payment_type,
        channel=nr.channel,
        total_discount=(
            nr.total_discount if nr.total_discount is not None
            else sum(it.discount for it in nr.items)
        ),
        receipt_url=nr.receipt_url,
        raw=nr.raw or {},
    )
    db.add(receipt)
    for it in nr.items:
        db.add(ReceiptItem(
            receipt=receipt, name=it.name, barcode=it.barcode, product_code=it.product_code or it.barcode,
            category_raw=it.category, quantity=it.quantity, unit=it.unit,
            unit_price=it.unit_price, total_price=it.total_price, discount=it.discount,
        ))
    db.flush()
    recalculate_receipt(db, receipt)
    apply_known_categories(db, receipt)
    try:
        match_receipt(db, receipt)  # ROADMAP.md Крок 2 auto-tick — best-effort
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    db.refresh(receipt)
    return receipt


def _persist_receipt(db: Session, account: Account, nr: NormalizedReceipt) -> Receipt:
    """Atomic manual-ingestion duplicate check followed by persistence."""
    if account.store.code != "scanner":
        return _persist_receipt_unlocked(db, account, nr)
    with ingestion_lock():
        return _persist_receipt_unlocked(db, account, nr)


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #
class UploadResult(BaseModel):
    kind: str                       # "receipt" | "document"
    ratio: float | None = None
    # receipt branch
    scan_job_id: str | None = None
    receipt_id: str | None = None
    method: str | None = None
    confidence: str | None = None
    needs_review: bool = False
    duplicate: bool = False
    # Rescan bookkeeping: the receipt this run replaced, or the one it left in
    # place because the rerun came back with nothing better.
    replaced_receipt_id: str | None = None
    kept_receipt_id: str | None = None
    # document branch
    document_id: str | None = None
    storage: str | None = None


async def _handle_receipt(db: Session, data: bytes, filename: str | None, media_type: str,
                          *, fiscal_override=None, ai_first: bool = False,
                          ai_after: tuple[str, str] | None = None,
                          qr_scan=None, job: "ScanJob | None" = None,
                          ai_images: list[bytes] | None = None,
                          uploader: str | None = None) -> UploadResult:
    from app.services import ai  # receipt branch only

    if job is None:
        job = ScanJob(source_filename=filename, status=ScanStatus.pending)
        db.add(job)
        db.commit()
        db.refresh(job)
        detail = f"{filename or 'фото'} · {len(data) // 1024} КБ"
        if uploader:
            detail += f" · від {uploader}"
        scan_jobs.record_event(db, job, "upload", detail=detail)
    SCAN_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    scan_input_path(job.id).write_bytes(data)

    # A receipt shot in parts goes to the model as its own frames, not as the
    # glued picture: the seam is an approximation, the originals are not. The
    # local stages keep working on `data`, which is what the glue is good for.
    vision_frames = list(ai_images) if ai_images else [data]

    async def _ai_ocr() -> AiReading:
        try:
            reading = await ai.ocr_receipt(db, vision_frames[0], media_type,
                                           start_after=ai_after,
                                           extra_images=vision_frames[1:] or None)
        except Exception as exc:  # noqa: BLE001
            logger.info("AI OCR unavailable: %s", exc)
            return AiReading(None)
        return AiReading(reading.data, service=reading.service, model=reading.model,
                         steps=reading.steps, attempts=reading.attempts)

    def _known_receipt(fiscal) -> str | None:
        found = find_receipt_by_fiscal(db, fiscal)
        return found.id if found is not None else None

    try:
        outcome: ScanOutcome = await scan_receipt_bytes(
            data,
            media_type,
            ai_ocr=_ai_ocr,
            tax_cabinet_token=tax_settings.get_token(db),
            fiscal_override=fiscal_override,
            ai_first=ai_first,
            known_receipt=_known_receipt,
            qr_scan=qr_scan,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan pipeline crashed")
        job.status = ScanStatus.failed
        job.error = str(exc)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Помилка розпізнавання: {exc}") from exc

    job.method = outcome.method
    job.confidence = outcome.confidence
    job.raw_ocr = {
        # The history outlives the run that is being recorded: a re-scan of the
        # same photo is another entry in this row's life, not a fresh row.
        "events": (job.raw_ocr or {}).get("events") or [],
        "notes": outcome.notes,
        # Which AI step read it, and every step this run spent getting there —
        # the anchor both UIs escalate from, and the answer to "why did this
        # receipt cost two model calls".
        "ai": ({"service": outcome.ai_service, "model": outcome.ai_model,
                "steps": outcome.ai_steps} if outcome.ai_service else None),
        # Stage-by-stage record of this run: what ran, what it found, what it
        # cost. Read back per receipt in both UIs, and aggregated by
        # /scans/stats — the only way to tell an expensive photo from an
        # expensive route.
        "trace": [dataclasses.asdict(step) for step in outcome.trace],
        "fiscal_url": outcome.fiscal.raw_url if outcome.fiscal else None,
        "qr_state": outcome.qr_state,
        "ocr_available": outcome.ocr_available,
        "reconciliation": (
            {"ok": outcome.reconciliation.ok, "delta": outcome.reconciliation.delta,
             "reason": outcome.reconciliation.reason}
            if outcome.reconciliation else None
        ),
    }

    for step in outcome.trace:
        scan_jobs.record_event(db, job, step.stage, actor="auto", outcome=step.outcome,
                               detail=step.detail, ms=step.ms,
                               tokens_in=step.tokens_in, tokens_out=step.tokens_out)

    receipt_id: str | None = None
    duplicate = False
    if outcome.duplicate_receipt_id:
        # The QR named a document already on file — link this photo to that
        # receipt and let it contribute whatever the receipt still lacks.
        receipt = db.get(Receipt, outcome.duplicate_receipt_id)
        receipt_id, duplicate = outcome.duplicate_receipt_id, True
        job.receipt_id = receipt_id
        job.status = ScanStatus.parsed
        job.method = outcome.method = "qr · дубль"
        if receipt is not None:
            if not receipt.receipt_url and outcome.fiscal:
                receipt.receipt_url = outcome.fiscal.raw_url
            attach_scan_photo(db, receipt, data, media_type)
    elif outcome.receipt is not None and outcome.confidence == "high":
        account = _scanner_account(db)
        duplicate = db.execute(select(Receipt.id).where(
            Receipt.account_id == account.id,
            Receipt.external_id == outcome.receipt.external_id,
        )).scalar_one_or_none() is not None
        receipt = _persist_receipt(db, account, outcome.receipt)
        duplicate = duplicate or receipt.external_id != outcome.receipt.external_id
        attach_scan_photo(db, receipt, data, media_type)
        receipt_id = receipt.id
        job.receipt_id = receipt_id
        job.status = ScanStatus.parsed
    elif outcome.receipt is not None:
        job.status = ScanStatus.needs_review
        job.raw_ocr = {**job.raw_ocr, "draft": _draft_dict(outcome.receipt)}
    else:
        job.status = ScanStatus.failed

    # Feed the template-authoring backlog when we only got through generic/AI.
    if outcome.method in ("generic", "ai") and outcome.receipt is not None:
        db.add(ScanSample(
            method=outcome.method,
            store_guess=outcome.receipt.shop_name,
            ocr_text=(outcome.receipt.raw or {}).get("ocr_excerpt"),
            structured=_draft_dict(outcome.receipt),
        ))
    db.commit()

    return UploadResult(
        kind="receipt", ratio=None, scan_job_id=job.id, receipt_id=receipt_id,
        method=outcome.method, confidence=outcome.confidence,
        needs_review=job.status == ScanStatus.needs_review,
        duplicate=duplicate,
    )


def _draft_dict(nr: NormalizedReceipt) -> dict:
    return {
        "shop_name": nr.shop_name,
        "purchased_at": nr.purchased_at.isoformat() if nr.purchased_at else None,
        "total_amount": nr.total_amount,
        "total_discount": nr.total_discount,
        "items": [
            {"name": it.name, "quantity": it.quantity, "unit_price": it.unit_price,
             "total_price": it.total_price, "discount": it.discount}
            for it in nr.items
        ],
    }


class SeriesUploadOut(BaseModel):
    """What a batch of photos turned into.

    `kind` is "one" (glued into a single receipt), "separate" (several receipts,
    one row each) or "ask" — the QR codes did not settle it and the row is
    waiting for the user to say which it is.
    """

    kind: str
    merged: bool
    needs_choice: bool = False
    note: str = ""
    scan_job_id: str | None = None
    results: list[UploadResult]


@router.post("/upload-series", response_model=SeriesUploadOut)
async def upload_series(
    files: list[UploadFile] = File(...),
    scan_job_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Photos that belong together → the receipts they actually are.

    Frames of one long receipt are merged by their overlapping lines, in the
    order given (the order they were shot in). Photos of *different* receipts —
    told apart by their fiscal QR codes — are filed separately instead.
    `scan_job_id` continues an existing scan ("add one more photo").
    """
    from app.services import series_scan

    parts: list[tuple[bytes, str, str | None]] = []
    cfg = inbox_svc.load_inbox_config(db)
    for file in files:
        data = await file.read()
        if not data:
            continue
        if len(data) > _MAX_BYTES_HARD or len(data) > cfg.max_file_mb * 1024 * 1024:
            raise HTTPException(status_code=400,
                                detail=f"Файл {file.filename} більший за ліміт {cfg.max_file_mb} МБ")
        parts.append((data, file.content_type or "image/jpeg", file.filename))
    if not parts:
        raise HTTPException(status_code=400, detail="Порожній запит")

    job = series_scan.open_series(db, scan_job_id) if scan_job_id else None
    if scan_job_id and job is None:
        raise HTTPException(status_code=404, detail="Серію не знайдено або вона вже збережена")
    try:
        outcome = await series_scan.scan_batch(db, parts, job=job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _series_out(outcome)


def _series_out(outcome) -> "SeriesUploadOut":
    return SeriesUploadOut(
        kind=outcome.kind, merged=outcome.merged, needs_choice=outcome.needs_choice,
        note=outcome.note, scan_job_id=outcome.scan_job_id, results=outcome.results,
    )


@router.post("/scans/{job_id}/resolve-series", response_model=SeriesUploadOut)
async def resolve_series(job_id: str, choice: str = Query(...),
                         db: Session = Depends(get_db)):
    """Answer the question a batch asked: `choice=one` glues its frames into a
    single receipt, `choice=separate` files each frame on its own."""
    from app.services import series_scan

    job = db.get(ScanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Сканування не знайдено")
    scan_jobs.record_event(db, job, "series",
                           detail="один чек" if choice == "one" else "різні чеки")
    try:
        return _series_out(await series_scan.resolve(db, job, choice))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/upload", response_model=UploadResult)
async def upload(file: UploadFile, db: Session = Depends(get_db)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Порожній файл")
    if len(data) > _MAX_BYTES_HARD:
        raise HTTPException(status_code=400, detail="Файл завеликий")

    cfg = inbox_svc.load_inbox_config(db)
    if len(data) > cfg.max_file_mb * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"Файл більший за ліміт {cfg.max_file_mb} МБ")

    media_type = file.content_type or "image/jpeg"
    try:
        shape = await asyncio.to_thread(image_dimensions, data)
    except Exception as exc:  # noqa: BLE001 — not a decodable image
        raise HTTPException(status_code=400, detail=f"Не вдалося прочитати зображення: {exc}") from exc

    try:
        color = await asyncio.to_thread(color_stats, data)
    except Exception:  # noqa: BLE001 — colour signal is optional, shape still works
        color = None

    kind = route(
        shape, color,
        receipt_ratio=cfg.receipt_ratio,
        color_max_saturation=cfg.color_max_saturation,
        color_max_nongray_fraction=cfg.color_max_nongray_fraction,
    )
    if kind == "receipt":
        res = await _handle_receipt(db, data, file.filename, media_type)
        res.ratio = round(shape.ratio, 3)
        return res

    doc = await inbox_svc.store_document(
        db, data=data, original_filename=file.filename,
        content_type=media_type, shape=shape,
    )
    return UploadResult(kind="document", ratio=round(shape.ratio, 3),
                        document_id=doc.id, storage=doc.storage)


# --------------------------------------------------------------------------- #
# Document inbox
# --------------------------------------------------------------------------- #
class InboxDocOut(BaseModel):
    id: str
    storage: str
    original_filename: str | None
    content_type: str | None
    width: int | None
    height: int | None
    ratio: float | None
    status: str
    final_name: str | None
    target_folder_id: str | None
    created_at: dt.datetime
    processed_at: dt.datetime | None
    file_url: str | None = None

    @classmethod
    def of(cls, d: InboxDocument) -> "InboxDocOut":
        return cls(
            id=d.id, storage=d.storage, original_filename=d.original_filename,
            content_type=d.content_type, width=d.width, height=d.height, ratio=d.ratio,
            status=d.status.value if hasattr(d.status, "value") else str(d.status),
            final_name=d.final_name, target_folder_id=d.target_folder_id,
            created_at=d.created_at, processed_at=d.processed_at,
            file_url=f"/api/inbox/{d.id}/file" if d.storage == "local" else None,
        )


@router.get("", response_model=list[InboxDocOut])
def list_inbox(db: Session = Depends(get_db), include_done: bool = False):
    stmt = select(InboxDocument).order_by(InboxDocument.created_at.desc())
    if not include_done:
        stmt = stmt.where(InboxDocument.status == InboxStatus.unprocessed)
    return [InboxDocOut.of(d) for d in db.execute(stmt).scalars().all()]


@router.get("/{doc_id}/file")
def inbox_file(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(InboxDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не знайдено")
    path = inbox_svc.local_path(doc)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail="Файл недоступний локально")
    return FileResponse(path, media_type=doc.content_type or "application/octet-stream")


class ProcessIn(BaseModel):
    final_name: str
    target_folder_id: str | None = None


@router.post("/{doc_id}/process", response_model=InboxDocOut)
async def process_inbox(doc_id: str, payload: ProcessIn, db: Session = Depends(get_db)):
    doc = db.get(InboxDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Документ не знайдено")
    if doc.status == InboxStatus.done:
        raise HTTPException(status_code=400, detail="Документ вже оброблено")
    try:
        doc = await inbox_svc.process_document(
            db, doc, final_name=payload.final_name, target_folder_id=payload.target_folder_id
        )
    except inbox_svc.DriveNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return InboxDocOut.of(doc)


@router.delete("/{doc_id}", status_code=204)
def delete_inbox(doc_id: str, db: Session = Depends(get_db)):
    doc = db.get(InboxDocument, doc_id)
    if doc is None:
        return
    path = inbox_svc.local_path(doc)
    if path is not None:
        Path(path).unlink(missing_ok=True)
    db.delete(doc)
    db.commit()


@router.get("/drive-tree")
async def drive_tree(parent: str | None = Query(default=None), db: Session = Depends(get_db)):
    try:
        return await inbox_svc.drive_tree(db, parent)
    except inbox_svc.DriveNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class DriveFolderIn(BaseModel):
    name: str
    parent_id: str | None = None
    color: str | None = None


@router.post("/drive-folder")
async def drive_folder(payload: DriveFolderIn, db: Session = Depends(get_db)):
    try:
        return await inbox_svc.create_drive_folder(
            db, name=payload.name, parent_id=payload.parent_id, color=payload.color
        )
    except inbox_svc.DriveNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# Google Drive — OAuth connect flow (ROADMAP.md Крок 3)
# --------------------------------------------------------------------------- #
class DriveStatusOut(BaseModel):
    configured: bool                    # a client id + secret are available (settings or env)
    connected: bool                     # a usable refresh token is stored
    redirect_uri: str
    client_id: str = ""
    client_secret_masked: str | None = None
    client_source: str = "none"         # "settings" | "env" | "none"
    root_folder_id: str | None = None
    root_folder_name: str | None = None
    root_folder_color: str | None = None
    inbox_folder_id: str | None = None
    inbox_folder_name: str | None = None
    inbox_folder_color: str | None = None


def _drive_status(db: Session) -> DriveStatusOut:
    cfg = inbox_svc.load_drive_config(db)
    hint = drive_svc.client_hint(db)
    return DriveStatusOut(
        configured=drive_svc.oauth_configured(db),
        connected=cfg.connected,
        redirect_uri=drive_svc.redirect_uri(),
        client_id=hint["client_id"],
        client_secret_masked=hint["client_secret_masked"],
        client_source=hint["source"],
        root_folder_id=cfg.root_folder_id,
        root_folder_name=cfg.root_folder_name,
        root_folder_color=cfg.root_folder_color,
        inbox_folder_id=cfg.inbox_folder_id,
        inbox_folder_name=cfg.inbox_folder_name,
        inbox_folder_color=cfg.inbox_folder_color,
    )


@router.get("/drive/status", response_model=DriveStatusOut)
def drive_status(db: Session = Depends(get_db)):
    return _drive_status(db)


@router.get("/drive/color-palette")
async def drive_color_palette(db: Session = Depends(get_db)):
    """Allowed folder colours (Drive snaps anything else). Falls back to the
    built-in legacy palette when Drive isn't reachable."""
    if not inbox_svc.load_drive_config(db).connected:
        return {"colors": list(drive_svc.FOLDER_COLORS), "default": drive_svc.DEFAULT_FOLDER_COLOR}
    return {"colors": await drive_svc.get_color_palette(db),
            "default": drive_svc.DEFAULT_FOLDER_COLOR}


class DriveFoldersIn(BaseModel):
    root_folder_id: str | None = None
    root_folder_name: str | None = None
    root_folder_color: str | None = None
    inbox_folder_id: str | None = None
    inbox_folder_name: str | None = None
    inbox_folder_color: str | None = None
    clear_root: bool = False
    clear_inbox: bool = False


@router.put("/drive/folders", response_model=DriveStatusOut)
def put_drive_folders(payload: DriveFoldersIn, db: Session = Depends(get_db)):
    """Set the base ("коренева") and/or inbox ("Необроблені") folders. Only the
    fields provided are changed; the browser passes the folder name + colour so
    the UI can show them without a second lookup."""
    cfg = inbox_svc.load_drive_config(db)
    if payload.clear_root:
        cfg.root_folder_id = cfg.root_folder_name = cfg.root_folder_color = None
    elif payload.root_folder_id is not None:
        cfg.root_folder_id = payload.root_folder_id
        cfg.root_folder_name = payload.root_folder_name
        cfg.root_folder_color = payload.root_folder_color
    if payload.clear_inbox:
        cfg.inbox_folder_id = cfg.inbox_folder_name = cfg.inbox_folder_color = None
    elif payload.inbox_folder_id is not None:
        cfg.inbox_folder_id = payload.inbox_folder_id
        cfg.inbox_folder_name = payload.inbox_folder_name
        cfg.inbox_folder_color = payload.inbox_folder_color
    inbox_svc.save_config(db, inbox_svc.DRIVE_CONFIG_KEY, cfg)
    return _drive_status(db)


class DriveFolderColorIn(BaseModel):
    folder_id: str
    color: str | None = None   # None / falsy → reset to Drive default


@router.put("/drive/folder-color", response_model=DriveStatusOut)
async def put_drive_folder_color(payload: DriveFolderColorIn, db: Session = Depends(get_db)):
    try:
        await inbox_svc.set_folder_color(db, payload.folder_id, payload.color)
    except inbox_svc.DriveNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _drive_status(db)


class DriveClientIn(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None  # write-only; never returned


@router.put("/drive/client", response_model=DriveStatusOut)
def put_drive_client(payload: DriveClientIn, db: Session = Depends(get_db)):
    drive_svc.set_client(db, client_id=payload.client_id, client_secret=payload.client_secret)
    return _drive_status(db)


@router.get("/drive/authorize")
def drive_authorize(db: Session = Depends(get_db)):
    """Returns the Google consent URL to open in a new tab."""
    import secrets

    try:
        state = secrets.token_urlsafe(24)
        url = drive_svc.build_authorize_url(db, state)
    except drive_svc.DriveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    oauth_state.put(state, {"flow": "drive"})
    return {"authorize_url": url}


def _drive_page(title: str, ok: bool) -> HTMLResponse:
    color = "#4ade80" if ok else "#f87171"
    html = (
        f'<!doctype html><meta charset="utf-8"><title>{title}</title>'
        '<style>body{font-family:-apple-system,sans-serif;background:#0f172a;'
        'color:#e2e8f0;display:flex;align-items:center;justify-content:center;'
        f'height:100vh;margin:0}}h2{{color:{color}}}</style>'
        f'<div style="text-align:center"><h2>{title}</h2>'
        '<p>Можна закрити цю вкладку і повернутися до застосунку.</p></div>'
    )
    return HTMLResponse(html, status_code=200 if ok else 400)


@router.get("/drive/callback")
async def drive_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return _drive_page(f"Помилка авторизації Google: {error}", ok=False)
    if not code or not state or oauth_state.pop(state) is None:
        return _drive_page("Сесія авторизації застаріла — спробуйте ще раз", ok=False)
    try:
        await drive_svc.exchange_code(db, code)
    except drive_svc.DriveError as exc:
        return _drive_page(str(exc), ok=False)
    # Mark connected + remember it in the (non-secret) drive_config blob.
    cfg = inbox_svc.load_drive_config(db)
    cfg.connected = True
    inbox_svc.save_config(db, inbox_svc.DRIVE_CONFIG_KEY, cfg)
    return _drive_page("✅ Google Drive під'єднано", ok=True)


@router.post("/drive/disconnect", response_model=DriveStatusOut)
async def drive_disconnect(db: Session = Depends(get_db)):
    await drive_svc.revoke(db)
    cfg = inbox_svc.load_drive_config(db)
    cfg.connected = False
    inbox_svc.save_config(db, inbox_svc.DRIVE_CONFIG_KEY, cfg)
    return _drive_status(db)


# --------------------------------------------------------------------------- #
# Scan journal + stats
# --------------------------------------------------------------------------- #
class ScanJobOut(BaseModel):
    id: str
    source_filename: str | None
    status: str
    method: str | None
    confidence: str | None
    receipt_id: str | None
    raw_ocr: dict
    error: str | None
    created_at: dt.datetime
    input_url: str | None = None
    # Frames of a batch, in the order they were shot — the row shows the series
    # instead of pretending a multi-photo receipt is one picture.
    part_urls: list[str] = []
    # The batch is waiting to be told whether it is one receipt or several.
    needs_choice: bool = False
    # What the scan turned out to be about — from the saved receipt, or from the
    # draft when it is still awaiting review. Lets the journal be read as a list
    # of purchases (and duplicates spotted) without opening every row.
    shop_name: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    purchased_at: dt.datetime | None = None
    # True when this scan recorded a receipt that has since been deleted by hand.
    receipt_deleted: bool = False
    archived_at: dt.datetime | None = None
    # Set when another scan already filed this same receipt: the id of the first
    # one, so the journal can show which rows are copies of the same purchase.
    duplicate_of: str | None = None
    # "QR" / "ДПС кабінет" / "AI: Google Gemini / gemini-…" — what actually read
    # this photo, shown in both UIs so the user can tell a fiscal document from
    # a model's reading at a glance.
    recognised_by: str = ""
    # The next step of the OCR route after the one already spent, when there is
    # one: {"service", "model", "label"}. Absent once the route is exhausted —
    # that is what hides the "спробувати сильнішу AI" action.
    next_ai: dict | None = None


def _draft_purchased_at(draft: dict) -> dt.datetime | None:
    raw = draft.get("purchased_at")
    if not raw:
        return None
    try:
        return as_utc(dt.datetime.fromisoformat(str(raw)))
    except ValueError:
        return None


def _scan_job_out(job: ScanJob, receipt: Receipt | None,
                  duplicate_of: str | None = None,
                  next_ai: dict | None = None) -> ScanJobOut:
    draft = (job.raw_ocr or {}).get("draft") or {}
    return ScanJobOut(
        duplicate_of=duplicate_of,
        recognised_by=scan_jobs.recognised_by(job),
        next_ai=next_ai,
        id=job.id, source_filename=job.source_filename,
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        method=job.method, confidence=job.confidence,
        # Reported as "no receipt" so links and guards can't act on a ghost.
        receipt_id=receipt.id if receipt else None,
        receipt_deleted=bool(job.receipt_id) and receipt is None,
        raw_ocr=job.raw_ocr or {}, error=job.error, created_at=as_utc(job.created_at),
        input_url=f"/api/inbox/scan-input/{job.id}",
        part_urls=[f"/api/inbox/scan-input/{job.id}?part={n}"
                   for n in range(1, int((job.raw_ocr or {}).get("parts") or 0) + 1)],
        needs_choice=bool((job.raw_ocr or {}).get("pending_choice")),
        archived_at=as_utc(job.archived_at) if job.archived_at else None,
        shop_name=receipt.shop_name if receipt else draft.get("shop_name"),
        total_amount=receipt.total_amount if receipt else draft.get("total_amount"),
        currency=(receipt.currency if receipt else None) or "UAH",
        purchased_at=(as_utc(receipt.purchased_at) if receipt
                      else _draft_purchased_at(draft)),
    )


@router.get("/scans", response_model=list[ScanJobOut])
def list_scans(db: Session = Depends(get_db), limit: int = Query(default=50, le=200),
               include_archived: bool = Query(default=False)):
    query = select(ScanJob).order_by(ScanJob.created_at.desc())
    if not include_archived:
        query = query.where(ScanJob.archived_at.is_(None))
    rows = db.execute(query.limit(limit)).scalars().all()

    # `receipt_id` has no foreign key, so a receipt deleted by hand leaves the
    # column pointing at nothing. Look every one up instead of trusting it.
    claimed = [j.receipt_id for j in rows if j.receipt_id]
    receipts = {
        r.id: r for r in db.execute(
            select(Receipt).where(Receipt.id.in_(claimed))
        ).scalars().all()
    } if claimed else {}

    # Several photos of one purchase end up on one receipt. Point every later
    # scan at the first one that filed it, so the journal shows them as copies
    # rather than as unrelated rows that happen to look alike.
    first_scan: dict[str, str] = {}
    for job in sorted(rows, key=lambda j: j.created_at):
        if job.receipt_id in receipts:
            first_scan.setdefault(job.receipt_id, job.id)

    escalation = _escalation_resolver(db)
    return [
        _scan_job_out(
            j, receipts.get(j.receipt_id) if j.receipt_id else None,
            duplicate_of=(first_scan.get(j.receipt_id)
                          if j.receipt_id and first_scan.get(j.receipt_id) != j.id else None),
            next_ai=escalation(j),
        )
        for j in rows
    ]


def _escalation_resolver(db: Session):
    """The "next AI step" lookup, with the route config read once for the list.

    Every row resolves against the same current route: the step is found by the
    service+model pair the row used, so a route the user has reordered since
    still escalates in the order they now see in Settings.
    """
    from app.services import ai

    try:
        cfg = ai.load_config(db)
    except Exception:  # noqa: BLE001 — a broken AI config must not blank the journal
        logger.warning("could not read the AI config for the scan journal", exc_info=True)
        return lambda _job: None

    def resolve(job: ScanJob) -> dict | None:
        used = scan_jobs.used_ai_step(job)
        step = ai.next_ocr_step(db, used, cfg=cfg)
        if step is None:
            return None
        service, model, label = step
        return {"service": service, "model": model, "label": label}

    return resolve


@router.post("/scans/{job_id}/save-draft", response_model=ScanJobOut)
def save_scan_draft(job_id: str, db: Session = Depends(get_db)):
    """Accept a `needs_review` draft as it stands and file it as a receipt.

    Reviewing is judging whether the draft is right; correcting it afterwards is
    the receipt page's job, which already edits every field. So this only has to
    get the draft into a receipt the user can then open.
    """
    try:
        receipt, duplicate = scan_jobs.save_draft(db, job_id)
    except scan_jobs.ScanJobError as exc:
        raise HTTPException(status_code=404 if "не знайдено" in str(exc) else 409,
                            detail=str(exc)) from exc
    job = scan_jobs.get_job(db, job_id)
    out = _scan_job_out(job, receipt, next_ai=_escalation_resolver(db)(job))
    if duplicate:
        # The draft turned out to describe a purchase already on file; the row
        # now points at that receipt, which the draft has just filled out.
        out.duplicate_of = _first_scan_of(db, receipt.id, exclude=job_id)
    return out


def _first_scan_of(db: Session, receipt_id: str, *, exclude: str) -> str | None:
    return db.execute(
        select(ScanJob.id)
        .where(ScanJob.receipt_id == receipt_id, ScanJob.id != exclude)
        .order_by(ScanJob.created_at)
    ).scalars().first()


@router.post("/scans/{job_id}/archive", response_model=ScanJobOut)
def archive_scan(job_id: str, archived: bool = Query(default=True),
                 db: Session = Depends(get_db)):
    """Hide a finished scan from the working list (or bring it back)."""
    try:
        job = scan_jobs.set_archived(db, job_id, archived)
    except scan_jobs.ScanJobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    receipt = db.get(Receipt, job.receipt_id) if job.receipt_id else None
    return _scan_job_out(job, receipt, next_ai=_escalation_resolver(db)(job))

class BulkScanIn(BaseModel):
    """One action over several journal rows."""

    ids: list[str]
    # archive | unarchive | delete | rescan | rescan_ai | save_draft
    action: str
    # delete only: also drop the receipt a scan produced. Without it a scan that
    # already filed a receipt is refused rather than silently taking it along.
    with_receipt: bool = False


class BulkScanResultItem(BaseModel):
    id: str
    ok: bool
    detail: str = ""


class BulkScanOut(BaseModel):
    done: int
    failed: int
    results: list[BulkScanResultItem]
    queued: bool = False


# Rerunning recognition costs a model call per row, so a stray "select all" over
# a long journal is capped rather than being allowed to spend the budget.
BULK_MAX = 50


@router.post("/scans/bulk", response_model=BulkScanOut)
def bulk_scans(
    payload: BulkScanIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Apply one action to several scans, reporting each row separately.

    Rows are independent: one that cannot be archived (already gone) or rescanned
    (its image was never stored) must not stop the rest, so every row gets its own
    ok/detail instead of the whole batch failing on the first problem.
    """
    ids = list(dict.fromkeys(payload.ids))  # keep order, drop repeats
    if not ids:
        raise HTTPException(status_code=400, detail="Не вибрано жодного сканування")
    if len(ids) > BULK_MAX:
        raise HTTPException(status_code=400,
                            detail=f"За раз можна обробити не більше {BULK_MAX} сканувань")
    if payload.action not in {"archive", "unarchive", "delete", "rescan",
                              "rescan_ai", "save_draft"}:
        raise HTTPException(status_code=400, detail=f"Невідома дія {payload.action!r}")

    if payload.action in {"rescan", "rescan_ai"}:
        background.add_task(
            scan_jobs.run_rescan_batch,
            ids,
            ai_first=payload.action == "rescan_ai",
        )
        return BulkScanOut(
            done=0,
            failed=0,
            queued=True,
            results=[BulkScanResultItem(id=job_id, ok=True, detail="queued") for job_id in ids],
        )

    results: list[BulkScanResultItem] = []
    for job_id in ids:
        try:
            if payload.action in {"archive", "unarchive"}:
                scan_jobs.set_archived(db, job_id, payload.action == "archive")
            elif payload.action == "delete":
                scan_jobs.delete_job(db, job_id, with_receipt=payload.with_receipt)
            elif payload.action == "save_draft":
                scan_jobs.save_draft(db, job_id)
            results.append(BulkScanResultItem(id=job_id, ok=True))
        except (scan_jobs.ScanJobError, ValueError) as exc:
            results.append(BulkScanResultItem(id=job_id, ok=False, detail=str(exc)))
        except Exception as exc:  # noqa: BLE001 — one broken row, not a broken batch
            logger.exception("bulk %s failed for scan %s", payload.action, job_id)
            results.append(BulkScanResultItem(id=job_id, ok=False, detail=str(exc)))
    return BulkScanOut(
        done=sum(1 for r in results if r.ok),
        failed=sum(1 for r in results if not r.ok),
        results=results,
    )


@router.get("/scan-input/{job_id}")
def scan_input(job_id: str, part: int | None = Query(default=None, ge=1),
               db: Session = Depends(get_db)):
    """The image behind a row — the whole photo, or frame `part` of a batch."""
    if db.get(ScanJob, job_id) is None:
        raise HTTPException(404, "Сканування не знайдено")
    if part is not None:
        from app.services import series_scan

        path = series_scan.part_path(job_id, part)
        if not path.is_file():
            # Rows written before frames had their own names.
            path = scan_input_path(job_id if part == 1 else f"{job_id}-{part}")
    else:
        path = scan_input_path(job_id)
    if not path.is_file(): raise HTTPException(404, "Оригінал не збережено")
    return FileResponse(path, media_type="image/jpeg")


@router.delete("/scans/{job_id}", status_code=204)
def delete_scan(job_id: str, with_receipt: bool = Query(default=False),
                db: Session = Depends(get_db)):
    """Drop a scan from the journal. `with_receipt=true` also deletes the
    receipt it produced."""
    try:
        scan_jobs.delete_job(db, job_id, with_receipt=with_receipt)
    except scan_jobs.ScanJobError as exc:
        raise HTTPException(status_code=404 if "не знайдено" in str(exc) else 409, detail=str(exc)) from exc


@router.post("/scans/{job_id}/rescan", response_model=UploadResult)
async def rescan_scan(job_id: str, ai: bool = Query(default=False),
                      replace: bool = Query(default=False),
                      escalate: bool = Query(default=False),
                      db: Session = Depends(get_db)):
    """Re-run recognition on the stored image. `ai=true` skips local OCR and
    templates and goes straight to AI vision; `escalate=true` additionally
    resumes the AI route *after* the step already spent on this photo, reusing
    the QR verdict instead of paying for the ladder again; `replace=true` allows
    rerunning a scan that already produced a receipt, replacing it with the new
    result.

    A scan glued from several photos re-runs all of its frames — rerunning only
    the first one would silently turn a three-photo receipt into a one-photo one.
    """
    from app.services import series_scan

    try:
        job = db.get(ScanJob, job_id)
        if job is not None and series_scan.is_series(job) and not job.receipt_id:
            return await series_scan.rescan_series(db, job, ai_first=ai, escalate=escalate)
        return await scan_jobs.rescan_job(db, job_id, ai_first=ai, replace=replace,
                                          escalate=escalate)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except scan_jobs.ScanJobError as exc:
        raise HTTPException(status_code=404 if "не знайдено" in str(exc) else 409, detail=str(exc)) from exc


@router.get("/scans/stats")
def scan_stats(db: Session = Depends(get_db)):
    """How recognition is actually performing, across every scan on file.

    Three questions this has to answer, because each one changes a different
    decision: how often the local QR ladder wins (it is free — every scan it
    settles is a model call not made), which rung of that ladder pays off (the
    ladder is ordered by expected value and only real receipts can confirm that
    order), and which model reads receipts well enough to be worth its place in
    the route.
    """
    rows = db.execute(select(ScanJob.method, ScanJob.confidence, ScanJob.raw_ocr)).all()
    total = len(rows)
    by_method: dict[str, int] = {}
    qr_states: dict[str, int] = {}
    variants: dict[str, int] = {}
    stage_ms: dict[str, int] = {}
    models: dict[str, dict] = {}
    escalated = ai_scans = qr_ms = qr_decoded = 0

    for method, confidence, raw in rows:
        by_method[method or "none"] = by_method.get(method or "none", 0) + 1
        raw = raw or {}
        state = str(raw.get("qr_state") or "unknown")
        qr_states[state] = qr_states.get(state, 0) + 1
        steps = ((raw.get("ai") or {}).get("steps")) or []
        if len(steps) > 1:
            escalated += 1
        for step in raw.get("trace") or []:
            stage = str(step.get("stage") or "")
            stage_ms[stage] = stage_ms.get(stage, 0) + int(step.get("ms") or 0)
            if stage == "qr":
                qr_ms += int(step.get("ms") or 0)
                if step.get("outcome") == "ok":
                    qr_decoded += 1
                    # "zxing · roi_max_rgb · кадр 2" — the rung is the second part.
                    parts = [p.strip() for p in str(step.get("detail") or "").split("·")]
                    if len(parts) > 1:
                        variants[parts[1]] = variants.get(parts[1], 0) + 1
            elif stage == "ai":
                ai_scans += 1
                name = str(step.get("detail") or "").split("—")[0].strip() or "?"
                row = models.setdefault(name, {"calls": 0, "ok": 0, "ms": 0,
                                               "tokens_in": 0, "tokens_out": 0})
                row["calls"] += 1
                row["ok"] += 1 if step.get("outcome") == "ok" else 0
                row["ms"] += int(step.get("ms") or 0)
                row["tokens_in"] += int(step.get("tokens_in") or 0)
                row["tokens_out"] += int(step.get("tokens_out") or 0)

    traced = sum(1 for _m, _c, raw in rows if (raw or {}).get("trace"))
    ai_count = sum(n for m, n in by_method.items() if m and m.startswith("ai"))
    return {
        "total": total,
        "by_method": by_method,
        "ai_share": round(ai_count / total, 3) if total else 0.0,
        "saved_share": round(
            sum(1 for _m, c, _r in rows if c == "high") / total, 3) if total else 0.0,
        # Scans whose QR settled the receipt without any model call.
        "qr_states": qr_states,
        "qr_decoded_share": round(qr_decoded / traced, 3) if traced else 0.0,
        "qr_avg_ms": round(qr_ms / traced) if traced else 0,
        # Which rendering of the ladder decoded the code, most-used first.
        "qr_variants": dict(sorted(variants.items(), key=lambda kv: -kv[1])),
        "stage_ms": stage_ms,
        "ai_calls": ai_scans,
        "ai_escalated": escalated,
        "ai_models": {
            name: {**row, "avg_ms": round(row["ms"] / row["calls"]) if row["calls"] else 0}
            for name, row in sorted(models.items(), key=lambda kv: -kv[1]["calls"])
        },
        # Rows written before the trace existed carry none of this, so the
        # shares above are over `traced`, not over `total`.
        "traced": traced,
    }


# --------------------------------------------------------------------------- #
# Config (mirrors the /api/settings/ai pattern)
# --------------------------------------------------------------------------- #
@router.get("/config", response_model=inbox_svc.InboxConfig)
def get_inbox_config(db: Session = Depends(get_db)):
    return inbox_svc.load_inbox_config(db)


@router.put("/config", response_model=inbox_svc.InboxConfig)
def put_inbox_config(payload: inbox_svc.InboxConfig, db: Session = Depends(get_db)):
    inbox_svc.save_config(db, inbox_svc.INBOX_CONFIG_KEY, payload)
    return payload


@router.get("/drive-config", response_model=inbox_svc.DriveConfig)
def get_drive_config(db: Session = Depends(get_db)):
    return inbox_svc.load_drive_config(db)


@router.put("/drive-config", response_model=inbox_svc.DriveConfig)
def put_drive_config(payload: inbox_svc.DriveConfig, db: Session = Depends(get_db)):
    inbox_svc.save_config(db, inbox_svc.DRIVE_CONFIG_KEY, payload)
    return payload
