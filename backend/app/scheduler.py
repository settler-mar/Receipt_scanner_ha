"""A single periodic tick (every minute) checks which enabled Schedules are due
and runs a collection for their Account. Simpler and more robust than dynamically
adding/removing APScheduler jobs whenever accounts/schedules change through the API.

Cron expressions are evaluated in Europe/Kyiv local time (see core/time.py) —
`next_run_at` is always stored/compared as UTC.
"""
from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.adapters.registry import get_adapter
from app.core.time import KYIV, as_utc, next_cron_run
from app.db import SessionLocal
from app.integrations import OrderDeliveryUpdater, OrderTrackingAdapter
from app.models.models import Account, AccountStatus, Schedule, Store
from app.services.categorize import run_classification_batch
from app.services.collector import (
    collect_for_account,
    ensure_fresh_token,
    persist_rotated_access_token,
)
from app.services.fuel_prices import refresh_all_fuel_prices


async def _fiscal_queue_tick() -> None:
    """Automatically retry queued DPS receipts through the token API."""
    from app.api.inbox import _persist_receipt, _scanner_account
    from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
    from app.scanner.fiscal_qr import parse_fiscal_url
    from app.scanner.tax_cabinet import fetch_structured
    from app.services.tax_cabinet_settings import get_token
    db=SessionLocal()
    try:
        jobs=db.execute(select(FiscalQueueJob).where(FiscalQueueJob.status==FiscalQueueStatus.queued).limit(10)).scalars().all()
        token=get_token(db)
        for job in jobs:
            job.status=FiscalQueueStatus.processing; job.attempts+=1; db.commit()
            receipt=await fetch_structured(parse_fiscal_url(job.url),api_token=token)
            if receipt:
                saved=_persist_receipt(db,_scanner_account(db),receipt); job.status=FiscalQueueStatus.imported; job.receipt_id=saved.id; job.error=None
            else:
                job.status=FiscalQueueStatus.captcha; job.error="API ДПС недоступне — очікується CAPTCHA в адмінці"
            db.commit()
    except Exception:
        db.rollback(); logger.warning("Fiscal queue tick failed",exc_info=True)
    finally: db.close()

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def _tick() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    db = SessionLocal()
    try:
        due = db.execute(
            select(Schedule).where(Schedule.enabled.is_(True))
        ).scalars().all()

        for schedule in due:
            if schedule.next_run_at is None:
                schedule.next_run_at = next_cron_run(schedule.cron_expression, now)
                # Bug fixed 26.08.2026: this branch never committed, so next_run_at
                # never actually persisted — every tick saw None again and
                # recomputed it forever, so schedules never became "due". Every
                # account's next_run_at was still None after >24h of the scheduler
                # supposedly running (confirmed live) until this commit was added.
                db.commit()
                continue
            if as_utc(schedule.next_run_at) > now:
                continue

            account = db.get(Account, schedule.account_id)
            if account is None:
                continue

            # Manual-entry stores (scanner: receipts are pushed via photo
            # upload, never pulled) have no credential to collect with —
            # calling collect_for_account for them isn't "nothing to do", it's
            # an immediate AdapterError ("Немає токена") that wrongly flips
            # the account to needs_reauth on every single tick. Skip them —
            # there's genuinely nothing for the scheduler to fetch.
            if not get_adapter(account.store.code).capabilities["receipts"]:
                schedule.last_run_at = now
                schedule.next_run_at = next_cron_run(schedule.cron_expression, now)
                db.commit()
                continue

            # An account may be created before its one-time browser/token setup
            # is completed (AliExpress in particular). Do not turn that normal
            # setup state into a scheduled collection error every minute.
            if account.credential is None or not account.credential.access_token_enc:
                schedule.next_run_at = next_cron_run(schedule.cron_expression, now)
                db.commit()
                continue

            logger.info("Scheduled collection for account %s", account.label)
            await collect_for_account(db, account, trigger="schedule")

            schedule.last_run_at = now
            schedule.next_run_at = next_cron_run(schedule.cron_expression, now)
            db.commit()
    finally:
        db.close()


async def _classify_tick() -> None:
    """Background product-category classification (ROADMAP.md Крок 1). Chews
    through the queue of unknown product names one batch at a time; a no-op when
    no AI backend is configured on the Settings page."""
    db = SessionLocal()
    try:
        count = await run_classification_batch(db)
        if count:
            logger.info("Classified %d new product name(s)", count)
    except Exception:  # noqa: BLE001 — never let this kill the scheduler
        logger.warning("Classification tick failed", exc_info=True)
    finally:
        db.close()


async def _duplicate_tick() -> None:
    """Periodic cross-source duplicate search. The service owns the worker lock
    and idempotent candidate fingerprints, so this scheduled pass can be safely
    skipped when a manual/admin run is already active."""
    from app.services.duplicates import DuplicateRunAlreadyRunning, run_duplicate_search

    db = SessionLocal()
    try:
        run = run_duplicate_search(db, trigger="schedule")
        if run.candidates_new:
            logger.info("Duplicate search found %d new candidate group(s)", run.candidates_new)
    except DuplicateRunAlreadyRunning:
        logger.info("Duplicate search skipped: another run is active")
    except Exception:  # noqa: BLE001
        logger.warning("Duplicate search tick failed", exc_info=True)
    finally:
        db.close()


async def _exchange_rate_tick() -> None:
    """Fill missing exchange rates for existing non-UAH receipts in small batches."""
    from app.services.exchange_rates import backfill_missing_rates

    db = SessionLocal()
    try:
        result = backfill_missing_rates(db, limit=100)
        if result.updated or result.missing:
            logger.info(
                "Exchange-rate backfill checked=%d updated=%d missing=%d",
                result.checked,
                result.updated,
                result.missing,
            )
    except Exception:  # noqa: BLE001
        logger.warning("Exchange-rate backfill tick failed", exc_info=True)
    finally:
        db.close()


async def _dnipro_session_keepalive() -> None:
    """Renew Dnipro-M's ~24-minute web session like an open browser does.

    The long-lived identity cookie alone is not sufficient for cabinet XHRs;
    PHPSESSID/session_hash are rotated by normal authenticated activity. User
    collection schedules may run only twice a day, so keep the short cookie jar
    warm and persist every rotation into the encrypted credential.
    """
    db = SessionLocal()
    try:
        accounts = db.execute(
            select(Account)
            .join(Store)
            .where(Store.code == "dnipro-m", Account.status == AccountStatus.active)
        ).scalars().all()
        adapter = get_adapter("dnipro-m")
        for account in accounts:
            if account.credential is None or not account.credential.access_token_enc:
                continue
            extra = dict(account.credential.extra or {})
            try:
                access_token = await ensure_fresh_token(db, account, adapter)
                await adapter.get_balance(access_token, extra)
                if persist_rotated_access_token(account, extra):
                    db.commit()
            except Exception:  # noqa: BLE001 — collection will surface a real auth failure
                db.rollback()
                logger.warning("Dnipro-M session keepalive failed for %s", account.label, exc_info=True)
    finally:
        db.close()


async def _fuel_prices_tick(min_age: dt.timedelta | None = None) -> None:
    """Refresh cached station board prices (dashboard reads these, never live).
    Runs 3×/day — more often than user collection schedules, since pump prices
    move independently of when receipts are pulled."""
    db = SessionLocal()
    try:
        count = await refresh_all_fuel_prices(db, min_age=min_age)
        if count:
            logger.info("Refreshed fuel prices for %d account(s)", count)
    except Exception:  # noqa: BLE001 — never let this kill the scheduler
        logger.warning("Fuel-price tick failed", exc_info=True)
    finally:
        db.close()


async def _order_tracking_tick(store_code: str) -> None:
    """Refresh mutable delivery data for any tracking-capable integration.

    Only orders whose last known status is not terminal are considered. The
    adapter controls its lightweight remote reads; the reusable updater handles
    candidate selection and non-destructive status/tracking-number merging.
    """
    db = SessionLocal()
    try:
        accounts = db.execute(
            select(Account)
            .join(Store)
            .where(Store.code == store_code, Account.status == AccountStatus.active)
        ).scalars().all()
        adapter = get_adapter(store_code)
        if not isinstance(adapter, OrderTrackingAdapter):
            return
        updater = OrderDeliveryUpdater()

        for account in accounts:
            if account.credential is None or not account.credential.access_token_enc:
                continue
            active = updater.active_receipts(db, account)
            if not active:
                continue

            extra = dict(account.credential.extra or {})
            try:
                access_token = await ensure_fresh_token(db, account, adapter)
                updates = await adapter.refresh_order_tracking(
                    access_token,
                    extra,
                    updater.targets(active),
                )
                changed = updater.apply(active, updates)
                persist_rotated_access_token(account, extra)
                db.commit()
                logger.info(
                    "Refreshed %s delivery data for %s: %d checked, %d changed",
                    store_code,
                    account.label,
                    len(updates),
                    changed,
                )
            except Exception:  # noqa: BLE001 — maintenance must not invalidate a good session
                db.rollback()
                logger.warning(
                    "%s daily delivery refresh failed for %s",
                    store_code,
                    account.label,
                    exc_info=True,
                )
    finally:
        db.close()


def start_scheduler() -> None:
    scheduler.add_job(_tick, "interval", minutes=1, id="schedule_tick", replace_existing=True)
    scheduler.add_job(
        _classify_tick, "interval", minutes=10, id="classify_tick", replace_existing=True
    )
    scheduler.add_job(
        _duplicate_tick,
        "interval",
        hours=6,
        id="duplicate_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _exchange_rate_tick,
        "interval",
        hours=6,
        id="exchange_rate_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _dnipro_session_keepalive,
        "interval",
        minutes=10,
        id="dnipro_session_keepalive",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Fuel prices: 07:00 / 13:00 / 19:00 Kyiv. Plus a one-off catch-up ~1 min
    # after startup for anything not refreshed in the last 6h (so a redeploy
    # doesn't re-hit every station API).
    scheduler.add_job(
        _fuel_prices_tick,
        "cron",
        hour="7,13,19",
        minute=0,
        timezone=KYIV,
        id="fuel_prices_tick",
        replace_existing=True,
    )
    scheduler.add_job(
        _fuel_prices_tick,
        "date",
        run_date=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1),
        kwargs={"min_age": dt.timedelta(hours=6)},
        id="fuel_prices_catchup",
        replace_existing=True,
    )
    # Delivery status and parcel numbers are mutable independently of order
    # contents. Refresh them once per day, off-peak, without re-downloading
    # products or images and without creating a separate process/container.
    scheduler.add_job(
        _order_tracking_tick,
        "cron",
        hour=4,
        minute=30,
        timezone=KYIV,
        id="aliexpress_tracking_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        kwargs={"store_code": "aliexpress"},
    )
    scheduler.start()
