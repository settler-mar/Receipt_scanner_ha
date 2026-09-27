"""Runs one collection pass for an Account: call its adapter, upsert receipts,
record a CollectionRun. Used by both the manual "collect now" API and the scheduler.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from exceptiongroup import BaseExceptionGroup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import (
    ROTATED_ACCESS_TOKEN_KEY,
    AdapterError,
    NormalizedReceipt,
    NormalizedReceiptComponent,
    NotImplementedYet,
)
from app.adapters.registry import get_adapter
from app.adapters.metro import MetroSessionExpired, generate_cart_number
from app.core.credentials import upsert_credential
from app.core.security import decrypt, encrypt
from app.core.time import as_utc
from app.models.models import (
    Account,
    AccountStatus,
    CollectionRun,
    Receipt,
    ReceiptComponent,
    ReceiptItem,
    RunStatus,
)
from app.services.categorize import apply_known_categories
from app.services.fuel_prices import persist_fuel_prices
from app.services.shopping_list import match_receipt
from app.services.product_images import localize_product_images
from app.services.exchange_rates import rate_to_uah
from app.integrations import FullHistoryImport
from app.services import raw_responses
from app.services.reconciliation import recalculate_receipts

logger = logging.getLogger(__name__)


async def collect_for_account(
    db: Session,
    account: Account,
    trigger: str = "manual",
    adapter_options: dict | None = None,
    run: CollectionRun | None = None,
) -> CollectionRun:
    if run is None:
        run = CollectionRun(account_id=account.id, trigger=trigger, status=RunStatus.running)
        db.add(run)
        db.commit()
        db.refresh(run)

    extra: dict = {}
    attempt = 0
    try:
        while True:
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

                # Normal runs collect only the recent overlap. Explicit full imports
                # intentionally pass since=None so every adapter can re-read its history
                # and repair missing orders, line items, or warranty metadata.
                latest = db.execute(
                    select(Receipt.purchased_at)
                    .where(Receipt.account_id == account.id)
                    .order_by(Receipt.purchased_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                latest = as_utc(latest)
                force_all = bool((adapter_options or {}).get(FullHistoryImport.FORCE_ALL_OPTION))
                since = None if force_all else ((latest - dt.timedelta(days=1)) if latest else None)

                try:
                    normalized_receipts = await adapter.fetch_receipts(access_token, extra, since=since)
                except MetroSessionExpired:
                    # METRO sessions are tied to an anonymous installation. A stored
                    # card number is enough to register a fresh installation; use a
                    # generated cart number because no physical cart is being scanned.
                    card_barcode = extra.get("card_barcode")
                    if account.store.code != "metro" or not isinstance(card_barcode, str) or not card_barcode.strip():
                        raise
                    logger.info("Re-registering expired METRO session for account %s", account.id)
                    result = await adapter.login_with_device_registration(generate_cart_number(), card_barcode)
                    if not result.access_token:
                        raise AdapterError("METRO: повторна реєстрація не повернула токен")
                    upsert_credential(db, account, result)
                    access_token = result.access_token
                    extra = {**(account.credential.extra or {}), "phone_number": account.phone_number}
                    normalized_receipts = await adapter.fetch_receipts(access_token, extra, since=since)
                # A full-history import can contain thousands of CDN image URLs. Image
                # localization is best-effort and must not hold the receipt/raw-response
                # import behind slow or forbidden AliExpress media requests.
                if not force_all:
                    await localize_product_images(normalized_receipts)
                new_count = _persist_receipts(db, account, normalized_receipts, run.id)
                raw_responses.maybe_cleanup(db)

                run.status = RunStatus.success
                run.receipts_found = len(normalized_receipts)
                run.receipts_new = new_count
                account.status = AccountStatus.active
                account.last_error = None
                break

            except Exception as exc:  # noqa: BLE001 — surfaced to the UI via CollectionRun.error_message
                logger.exception("Collection failed for account %s", account.id)
                exc = _unwrap_exception_group(exc)
                if is_transient_network_failure(exc) and attempt < len(_TRANSIENT_NETWORK_RETRY_DELAYS):
                    delay = _TRANSIENT_NETWORK_RETRY_DELAYS[attempt]
                    attempt += 1
                    logger.warning(
                        "Transient network failure collecting account %s, retrying in %ss (attempt %s/%s): %s",
                        account.id, delay, attempt, len(_TRANSIENT_NETWORK_RETRY_DELAYS), exc,
                    )
                    await asyncio.sleep(delay)
                    continue

                auth_failure = isinstance(exc, AdapterError) or is_auth_failure(exc)
                message = str(exc) or exc.__class__.__name__
                if auth_failure and not isinstance(exc, AdapterError):
                    message = f"Сесія застаріла — потрібен повторний вхід ({message})"
                run.status = RunStatus.error
                run.error_message = message
                account.last_error = message
                if auth_failure:
                    account.status = AccountStatus.needs_reauth
                break

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


def start_collection(db: Session, account: Account, *, trigger: str = "manual") -> CollectionRun:
    """Create a persistent collection job without performing network I/O."""
    run = CollectionRun(account_id=account.id, trigger=trigger, status=RunStatus.running)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def full_history_options(db: Session, account: Account) -> dict:
    """Build the potentially large full-import payload in the worker."""
    history_loaded = bool(
        account.credential
        and (account.credential.extra or {}).get(FullHistoryImport.marker_key(account.store.code))
    )
    if account.store.code == "aliexpress" and not history_loaded:
        previous_full_run = db.execute(
            select(CollectionRun.id).where(
                CollectionRun.account_id == account.id,
                CollectionRun.trigger == "manual_all",
                CollectionRun.status == RunStatus.success,
            ).limit(1)
        ).scalar_one_or_none()
        if previous_full_run and account.credential is not None:
            account.credential.extra = {
                **(account.credential.extra or {}),
                FullHistoryImport.marker_key(account.store.code): True,
            }
            db.commit()
    known_order_ids = db.execute(
        select(Receipt.external_id).where(Receipt.account_id == account.id)
    ).scalars().all()
    detail_known_ids: list[str] = []
    if account.store.code == "aliexpress":
        detail_known_ids = [
            receipt.external_id
            for receipt in db.execute(
                select(Receipt).where(Receipt.account_id == account.id)
            ).scalars()
            if isinstance(receipt.raw, dict) and receipt.raw.get("_detail_responses")
        ]
    return FullHistoryImport.options(
        list(known_order_ids),
        refresh_details=account.store.code == "aliexpress",
        detail_known_keys=detail_known_ids,
    )


def run_collection_in_background(
    run_id: str,
    account_id: str,
    *,
    trigger: str = "manual",
    adapter_options: dict | None = None,
) -> None:
    """Run a collection in a worker thread with its own event loop/session."""
    from app.db import SessionLocal

    with SessionLocal() as db:
        account = db.get(Account, account_id)
        run = db.get(CollectionRun, run_id)
        if account is None or run is None:
            if run is not None:
                run.status = RunStatus.error
                run.error_message = "Акаунт або запуск збору не знайдено"
                run.finished_at = dt.datetime.now(dt.timezone.utc)
                db.commit()
            return
        if trigger == "manual_all" and adapter_options is None:
            adapter_options = full_history_options(db, account)
        asyncio.run(collect_for_account(
            db, account, trigger=trigger, adapter_options=adapter_options, run=run,
        ))


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
        new_count = _persist_receipts(db, account, normalized_receipts, run.id)
        raw_responses.maybe_cleanup(db)

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


def _unwrap_exception_group(exc: BaseException) -> BaseException:
    """Some adapters (e.g. silpo, via mcp's anyio task groups) can still let a
    BaseExceptionGroup through despite their own unwrapping. str(exc) on a group
    only shows the group's own opaque message ("unhandled errors in a TaskGroup"),
    not the actual failure, so drill down to the first leaf exception before it
    reaches error_message/is_auth_failure."""
    inner = exc
    while isinstance(inner, BaseExceptionGroup) and inner.exceptions:
        inner = inner.exceptions[0]
    return inner


_AUTH_STATUS_CODES = (401, 403)
_AUTH_HINTS = ("401", "403", "unauthorized", "forbidden")


def is_auth_failure(exc: BaseException) -> bool:
    """Does this failure mean "the stored session is no longer valid"?

    Adapters are supposed to translate an expired session into an AdapterError,
    but a raw `raise_for_status()` (httpx) or any other library exception can
    still reach the collector — and a 401 that is not recognised leaves the
    account looking perfectly healthy in the UI ("статус: активний", last_error
    empty) while every scheduled run fails. So classify by the HTTP status when
    the exception carries one, and fall back to the message text.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "code", None)
    if status in _AUTH_STATUS_CODES:
        return True
    if status is not None:
        return False
    text = str(exc).casefold()
    return any(hint in text for hint in _AUTH_HINTS)


# Seconds to wait before each retry of a transient network failure. Two
# retries (25s total) is enough to ride out a resolver hiccup without turning
# a scheduled run into a long-blocking one.
_TRANSIENT_NETWORK_RETRY_DELAYS = (5, 20)

_TRANSIENT_NETWORK_HINTS = (
    "could not resolve host",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "connection closed while reading from the driver",
    "connection reset by peer",
    "network is unreachable",
    "errno -2",
    "errno -3",
)


def is_transient_network_failure(exc: BaseException) -> bool:
    """DNS/connection-level failure (resolver hiccup, dropped socket) that says
    nothing about the provider or the stored session. Unrelated stores failing
    with this same shape at the same time is the signature of the host/container
    itself losing DNS or network for a few seconds — worth a short retry instead
    of flagging needs_reauth or leaving the account red until tomorrow's run.
    """
    text = str(exc).casefold()
    return any(hint in text for hint in _TRANSIENT_NETWORK_HINTS)


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


def _persist_receipts(
    db: Session,
    account: Account,
    normalized_receipts: list[NormalizedReceipt],
    collection_run_id: str = "manual",
) -> int:
    """Upserts normalized receipts (skipping ones already stored by external_id),
    returns how many were newly inserted."""
    new_count = 0
    touched: list[Receipt] = []

    def replace_components(receipt: Receipt, components: list[NormalizedReceiptComponent], *, has_source_items: bool = False) -> None:
        if not components:
            # A previous pass may have stored a source-missing correction. Once
            # a later provider response contains real lines, that correction is
            # stale and must not continue masking the new reconciliation.
            if has_source_items:
                receipt.components[:] = [
                    component for component in receipt.components
                    if component.code != "source_missing_lines"
                ]
            return
        receipt.components[:] = [
            ReceiptComponent(
                kind=component.kind,
                code=component.code,
                label=component.label,
                amount=component.amount,
                currency=component.currency,
                source=component.source,
                raw=component.raw,
            )
            for component in components
        ]

    for nr in normalized_receipts:
        if nr.raw and collection_run_id != "manual":
            try:
                raw_responses.store(
                    store_code=account.store.code,
                    account_id=account.id,
                    collection_run_id=collection_run_id,
                    external_id=nr.external_id,
                    payload=nr.raw,
                    captured_at=dt.datetime.now(dt.timezone.utc),
                )
            except Exception:  # noqa: BLE001 — raw debugging must not break imports
                logger.warning("Could not store raw response for %s", account.store.code, exc_info=True)
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
            if nr.total_adjustment is not None:
                existing.total_adjustment = nr.total_adjustment
                existing.adjustment_reason = nr.adjustment_reason
            replace_components(existing, nr.components, has_source_items=bool(nr.items))
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
                        product_code=item.product_code or item.barcode,
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
                        # Some pharmacy providers abbreviate product titles on
                        # the first pass and return the full title later. A
                        # one-to-one amount/quantity match is safer than
                        # appending a duplicate line in that case.
                        target = next(
                            (row for row in unmatched
                             if round(float(row.total_price or 0), 2) == round(float(item.total_price or 0), 2)
                             and round(float(row.quantity or 0), 3) == round(float(item.quantity or 0), 3)),
                            None,
                        )
                    if target is None:
                        # A later collection can contain additional invoice
                        # lines (for example, METRO's first parser only kept
                        # rows with a particular separator). Preserve the
                        # existing ReceiptItem ids, but append genuinely new
                        # lines so linked shopping-list rows remain valid and
                        # can be matched below.
                        existing.items.append(
                            ReceiptItem(
                                name=item.name,
                                barcode=item.barcode,
                                product_code=item.product_code or item.barcode,
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
                        )
                        continue
                    if target in unmatched:
                        unmatched.remove(target)
                    # Adapters can improve normalization after a receipt was
                    # first imported (for example METRO initially exposed net
                    # PDF prices and now distributes aggregate VAT).  A normal
                    # collection pass must repair those persisted line values.
                    target.quantity = item.quantity
                    if item.product_code:
                        target.product_code = item.product_code
                    elif not target.product_code and item.barcode:
                        target.product_code = item.barcode
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
            # Some sources (Rozetka) return warranty status per product. Once
            # every imported line has been checked, keep the receipt aggregate
            # in sync as well, including clearing a stale previous `true`.
            if nr.items and all(item.warranty_checked_at is not None for item in nr.items):
                existing.has_warranty = any(bool(item.has_warranty) for item in nr.items)
            elif nr.has_warranty:
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
            total_adjustment=nr.total_adjustment or 0,
            adjustment_reason=nr.adjustment_reason,
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
        receipt.components = [
            ReceiptComponent(
                kind=component.kind,
                code=component.code,
                label=component.label,
                amount=component.amount,
                currency=component.currency,
                source=component.source,
                raw=component.raw,
            )
            for component in nr.components
        ]
        db.add(receipt)
        for it in nr.items:
            db.add(
                ReceiptItem(
                    receipt=receipt,
                    name=it.name,
                    barcode=it.barcode,
                    product_code=it.product_code or it.barcode,
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
    recalculate_receipts(db, touched)
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
