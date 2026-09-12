"""Runs one collection pass for an Account: call its adapter, upsert receipts,
record a CollectionRun. Used by both the manual "collect now" API and the scheduler.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import (
    ROTATED_ACCESS_TOKEN_KEY,
    AdapterError,
    NormalizedReceipt,
    NotImplementedYet,
)
from app.adapters.registry import get_adapter
from app.core.security import decrypt, encrypt
from app.core.time import as_utc
from app.models.models import (
    Account,
    AccountStatus,
    CollectionRun,
    Receipt,
    ReceiptItem,
    RunStatus,
)
from app.services.categorize import apply_known_categories
from app.services.fuel_prices import persist_fuel_prices
from app.services.shopping_list import match_receipt
from app.services.product_images import localize_product_images
from app.services.exchange_rates import rate_to_uah
from app.integrations import FullHistoryImport

logger = logging.getLogger(__name__)


async def collect_for_account(
    db: Session,
    account: Account,
    trigger: str = "manual",
    adapter_options: dict | None = None,
) -> CollectionRun:
    run = CollectionRun(account_id=account.id, trigger=trigger, status=RunStatus.running)
    db.add(run)
    db.commit()
    db.refresh(run)

    extra: dict = {}
    try:
        if account.credential is None or not account.credential.access_token_enc:
            raise AdapterError("Немає токена — спершу пройдіть авторизацію для цього акаунта")

        adapter = get_adapter(account.store.code)
        access_token = await ensure_fresh_token(db, account, adapter)
        extra = dict(account.credential.extra or {})
        extra.setdefault("phone_number", account.phone_number)
        if adapter_options:
            extra.update(adapter_options)

        # Best-effort and independent of receipt collection below — do this first
        # so a store whose receipts endpoint is broken/blocked (e.g. АТБ's
        # Cloudflare block) still gets its balance refreshed.
        await _refresh_balance(account, adapter, access_token, extra)
        await _refresh_fuel_prices(db, account, adapter, access_token, extra)

        # Collect everything newer than the most recent receipt we already have,
        # with a bit of overlap in case of clock skew / same-day multiple purchases.
        latest = db.execute(
            select(Receipt.purchased_at)
            .where(Receipt.account_id == account.id)
            .order_by(Receipt.purchased_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        latest = as_utc(latest)
        since = (latest - dt.timedelta(days=1)) if latest else None

        normalized_receipts = await adapter.fetch_receipts(access_token, extra, since=since)
        await localize_product_images(normalized_receipts)
        new_count = _persist_receipts(db, account, normalized_receipts)

        run.status = RunStatus.success
        run.receipts_found = len(normalized_receipts)
        run.receipts_new = new_count
        account.status = AccountStatus.active
        account.last_error = None

    except Exception as exc:  # noqa: BLE001 — surfaced to the UI via CollectionRun.error_message
        logger.exception("Collection failed for account %s", account.id)
        run.status = RunStatus.error
        run.error_message = str(exc)
        account.last_error = str(exc)
        if isinstance(exc, AdapterError):
            account.status = AccountStatus.needs_reauth

    finally:
        persist_rotated_access_token(account, extra)
        history_complete = bool(extra.pop(FullHistoryImport.COMPLETE_RESULT, False))
        if history_complete and run.status == RunStatus.success and account.credential is not None:
            account.credential.extra = {
                **(account.credential.extra or {}),
                FullHistoryImport.marker_key(account.store.code): True,
            }
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(run)

    return run


def persist_rotated_access_token(account: Account, extra: dict) -> bool:
    """Encrypt a browser adapter's refreshed cookie jar into Credential.

    The reserved value is removed from the transient dictionary so the secret
    cannot accidentally be merged into the unencrypted Credential.extra JSON.
    """
    token = extra.pop(ROTATED_ACCESS_TOKEN_KEY, None)
    if not isinstance(token, str) or not token or account.credential is None:
        return False
    account.credential.access_token_enc = encrypt(token)
    return True


async def import_for_account(db: Session, account: Account, payload: dict) -> CollectionRun:
    """Same persistence path as collect_for_account, but from a manually-pasted
    raw API response instead of a live network call — for stores where automated
    collection is blocked (see AtbAdapter.parse_import)."""
    run = CollectionRun(account_id=account.id, trigger="import", status=RunStatus.running)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        adapter = get_adapter(account.store.code)
        normalized_receipts = adapter.parse_import(payload)
        await localize_product_images(normalized_receipts)
        new_count = _persist_receipts(db, account, normalized_receipts)

        run.status = RunStatus.success
        run.receipts_found = len(normalized_receipts)
        run.receipts_new = new_count
        account.last_error = None

    except Exception as exc:  # noqa: BLE001
        logger.exception("Import failed for account %s", account.id)
        run.status = RunStatus.error
        run.error_message = str(exc)
        account.last_error = str(exc)

    finally:
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        db.refresh(run)

    return run


async def _refresh_balance(account: Account, adapter, access_token: str, extra: dict) -> None:
    """Best-effort: cache the loyalty balance on Account so accounts.py's list
    endpoint can return it instantly instead of live-fetching per row. Silently
    skipped for stores that don't support it (see BaseAdapter.get_balance);
    any other failure is logged but must not fail the collection run itself."""
    try:
        balance = await adapter.get_balance(access_token, extra)
    except NotImplementedYet:
        return
    except AdapterError:
        logger.warning("Could not refresh balance for account %s", account.label, exc_info=True)
        return
    account.bonus_balance = balance.total
    account.bonus_balance_updated_at = dt.datetime.now(dt.timezone.utc)
    account.discount_percent = balance.discount_percent


async def _refresh_fuel_prices(db: Session, account: Account, adapter, access_token: str, extra: dict) -> None:
    """Best-effort price snapshot for fuel chains during every collection run."""
    try:
        prices = await adapter.get_fuel_prices(access_token, extra)
    except NotImplementedYet:
        return
    except AdapterError:
        logger.warning("Could not refresh fuel prices for account %s", account.label, exc_info=True)
        return
    persist_fuel_prices(db, account, prices)


def _persist_receipts(db: Session, account: Account, normalized_receipts: list[NormalizedReceipt]) -> int:
    """Upserts normalized receipts (skipping ones already stored by external_id),
    returns how many were newly inserted."""
    new_count = 0
    touched: list[Receipt] = []
    for nr in normalized_receipts:
        exchange_rate = nr.exchange_rate_to_uah
        if exchange_rate is None:
            exchange_rate = rate_to_uah(nr.currency, nr.purchased_at)
        existing = db.execute(
            select(Receipt).where(
                Receipt.account_id == account.id, Receipt.external_id == nr.external_id
            )
        ).scalar_one_or_none()
        if nr.excluded:
            if existing is not None:
                logger.info(
                    "Removing excluded receipt %s (%s)",
                    nr.external_id,
                    nr.exclusion_reason or "excluded by source",
                )
                db.delete(existing)
            continue
        if existing:
            # Some adapters receive a richer header on a later collection
            # pass (Foxtrot's order list carries the total while its detail
            # endpoint only carries line items). Repair incomplete rows without
            # creating duplicates.
            if nr.total_amount and existing.total_amount != nr.total_amount:
                existing.total_amount = nr.total_amount
            if nr.total_discount is not None and existing.total_discount != nr.total_discount:
                existing.total_discount = nr.total_discount
            if nr.bonuses is not None and existing.bonuses != nr.bonuses:
                existing.bonuses = nr.bonuses
            if nr.bonuses_used is not None and existing.bonuses_used != nr.bonuses_used:
                existing.bonuses_used = nr.bonuses_used
            if existing.channel != nr.channel:
                existing.channel = nr.channel
            if existing.currency != nr.currency:
                existing.currency = nr.currency
            if (nr.currency or "UAH").upper() == "UAH" and existing.exchange_rate_to_uah is not None:
                existing.exchange_rate_to_uah = None
            if exchange_rate is not None and existing.exchange_rate_to_uah != exchange_rate:
                existing.exchange_rate_to_uah = exchange_rate
            if not existing.items and nr.items:
                existing.items = [
                    ReceiptItem(
                        name=item.name,
                        barcode=item.barcode,
                        category_raw=item.category,
                        quantity=item.quantity,
                        unit=item.unit,
                        unit_price=item.unit_price,
                        total_price=item.total_price,
                        discount=item.discount,
                        product_url=item.product_url,
                        product_image_url=item.product_image_url,
                        product_image_source_url=item.product_image_source_url,
                        merchant_name=item.merchant_name,
                        product_image_urls=item.product_image_urls,
                        has_warranty=item.has_warranty,
                        warranty_text=item.warranty_text,
                        warranty_expires_at=item.warranty_expires_at,
                        warranty_checked_at=item.warranty_checked_at,
                    )
                    for item in nr.items
                ]
            elif existing.items and nr.items:
                unmatched = list(existing.items)
                for item in nr.items:
                    # Consume matches one by one: the same barcode can occur on
                    # several separate receipt lines (notably in METRO PDFs).
                    target = next(
                        (row for row in unmatched if item.barcode and row.barcode == item.barcode),
                        None,
                    )
                    if target is None:
                        target = next((row for row in unmatched if row.name == item.name), None)
                    if target is None:
                        continue
                    if target in unmatched:
                        unmatched.remove(target)
                    # Adapters can improve normalization after a receipt was
                    # first imported (for example METRO initially exposed net
                    # PDF prices and now distributes aggregate VAT).  A normal
                    # collection pass must repair those persisted line values.
                    target.quantity = item.quantity
                    target.unit = item.unit
                    target.unit_price = item.unit_price
                    target.total_price = item.total_price
                    target.discount = item.discount
                    if item.product_url:
                        target.product_url = item.product_url
                    if item.product_image_url:
                        target.product_image_url = item.product_image_url
                    if item.product_image_source_url:
                        target.product_image_source_url = item.product_image_source_url
                    if item.merchant_name:
                        target.merchant_name = item.merchant_name
                    if item.product_image_urls:
                        target.product_image_urls = item.product_image_urls
                    if item.warranty_checked_at is not None:
                        target.has_warranty = item.has_warranty
                        target.warranty_text = item.warranty_text
                        target.warranty_expires_at = item.warranty_expires_at
                        target.warranty_checked_at = item.warranty_checked_at
            if not existing.receipt_url and nr.receipt_url:
                existing.receipt_url = nr.receipt_url
            if nr.raw:
                existing.raw = nr.raw
            if nr.order_status is not None:
                existing.order_status = nr.order_status
            # A normal AliExpress list refresh deliberately skips tracking
            # pages. Never erase numbers collected by the daily deep refresh.
            if nr.tracking_numbers:
                existing.tracking_numbers = nr.tracking_numbers
            if nr.has_warranty:
                existing.has_warranty = True
            if nr.warranty_expires_at is not None:
                existing.warranty_expires_at = nr.warranty_expires_at
            touched.append(existing)
            continue

        receipt = Receipt(
            account_id=account.id,
            store_code=account.store.code,
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
            bonuses=nr.bonuses,
            # NOTE: bonuses_used was previously dropped here despite being carried on
            # NormalizedReceipt (Silpo/Epicentr set it) — fixed 26.08.2026, see RESEARCH.md.
            bonuses_used=nr.bonuses_used,
            receipt_url=nr.receipt_url,
            order_status=nr.order_status,
            tracking_numbers=nr.tracking_numbers,
            has_warranty=nr.has_warranty,
            warranty_expires_at=nr.warranty_expires_at,
            raw=nr.raw,
        )
        db.add(receipt)
        for it in nr.items:
            db.add(
                ReceiptItem(
                    receipt=receipt,
                    name=it.name,
                    barcode=it.barcode,
                    category_raw=it.category,
                    quantity=it.quantity,
                    unit=it.unit,
                    unit_price=it.unit_price,
                    total_price=it.total_price,
                    discount=it.discount,
                    product_url=it.product_url,
                    product_image_url=it.product_image_url,
                    product_image_source_url=it.product_image_source_url,
                    merchant_name=it.merchant_name,
                    product_image_urls=it.product_image_urls,
                    has_warranty=it.has_warranty,
                    warranty_text=it.warranty_text,
                    warranty_expires_at=it.warranty_expires_at,
                    warranty_checked_at=it.warranty_checked_at,
                )
            )
        touched.append(receipt)
        new_count += 1

    db.flush()
    for receipt in touched:
        apply_known_categories(db, receipt)
        # Deterministic shopping-list auto-tick (ROADMAP.md Крок 2) — best-effort,
        # must never fail a collection run.
        try:
            match_receipt(db, receipt)
        except Exception:  # noqa: BLE001
            logger.warning("Shopping-list match failed for receipt %s", receipt.id, exc_info=True)
    return new_count


async def ensure_fresh_token(db: Session, account: Account, adapter) -> str:
    """Returns a usable access_token, transparently refreshing it first if it's
    expired (or about to expire) and the adapter supports it. Persists any new
    tokens so the next run doesn't have to refresh again."""
    cred = account.credential
    expires_at = as_utc(cred.expires_at)
    expiring_soon = expires_at is not None and expires_at <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=2)

    # Don't attempt (and definitely don't fail on) a refresh the adapter doesn't
    # support — that would turn a merely-stale expires_at estimate into an
    # immediate needs_reauth, even when the token itself still works fine. ATB's
    # id_token is documented to keep working ~360 days regardless of the short
    # expires_in the token endpoint reports (see RESEARCH.md); Fora's server may
    # tolerate a slightly-expired accessToken too. Just hand back the existing
    # token and let the real API call be the judge — if it's actually dead,
    # fetch_receipts/get_balance will raise their own AdapterError from a real
    # 401/403, which still correctly flips the account to needs_reauth.
    if not expiring_soon or not adapter.capabilities.get("refresh"):
        return decrypt(cred.access_token_enc)

    logger.info("Refreshing expired token for account %s", account.label)
    refresh_token = decrypt(cred.refresh_token_enc)
    result = await adapter.refresh(dict(cred.extra or {}), refresh_token)

    if result.access_token:
        cred.access_token_enc = encrypt(result.access_token)
    if result.refresh_token:
        cred.refresh_token_enc = encrypt(result.refresh_token)
    if result.expires_at:
        cred.expires_at = result.expires_at
    if result.extra:
        cred.extra = {**(cred.extra or {}), **result.extra}
    db.commit()

    return decrypt(cred.access_token_enc)
