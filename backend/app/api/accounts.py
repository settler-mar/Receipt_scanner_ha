from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import datetime as dt
import json
import secrets

from app.adapters.base import AdapterError
from app.adapters.registry import get_adapter
from app.api.deps import get_account_or_404, get_db
from app.core import oauth_state
from app.core.config import settings
from app.core.credentials import upsert_credential
from app.core.pkce import pkce_pair
from app.core.security import decrypt, encrypt
from app.models.models import (
    Account,
    AccountStatus,
    AuthType,
    CollectionRun,
    Credential,
    Receipt,
    RunStatus,
    Schedule,
    Store,
)
from app.schemas.schemas import (
    AccountCreate,
    AccountOut,
    AccountUpdate,
    AliLoginClose,
    AliBrowserClick,
    AliBrowserKey,
    AliBrowserPointer,
    AliBrowserText,
    AliLoginOtp,
    AliLoginOut,
    AliLoginStart,
    CardLogin,
    DeviceUuidLogin,
    DeviceRegistrationLogin,
    FuelPriceOut,
    ImportPayload,
    LoginConfirm,
    LoginStart,
    LoyaltyCodeOut,
    PasswordLogin,
    PromLoginStart,
    RunOut,
    ScheduleUpdate,
    TokenBootstrap,
)
from app.services.collector import (
    collect_for_account,
    ensure_fresh_token,
    import_for_account,
    run_collection_in_background,
    start_collection,
)
from app.services.fuel_prices import persist_fuel_prices
from app.services import prom_login
from app.services import store_classes
from app.services.aliexpress_login import close_login as close_aliexpress_login
from app.services.aliexpress_login import check_manual_login as check_aliexpress_manual_login
from app.services.aliexpress_login import control_login_click as control_aliexpress_login_click
from app.services.aliexpress_login import control_login_key as control_aliexpress_login_key
from app.services.aliexpress_login import control_login_pointer as control_aliexpress_login_pointer
from app.services.aliexpress_login import control_login_text as control_aliexpress_login_text
from app.services.aliexpress_login import request_manual_takeover as request_aliexpress_manual_takeover
from app.services.aliexpress_login import get_login_progress as get_aliexpress_login_progress
from app.services.aliexpress_login import get_login_screenshot as get_aliexpress_login_screenshot
from app.services.aliexpress_login import start_login as start_aliexpress_login
from app.services.aliexpress_login import submit_otp as submit_aliexpress_otp
from app.services.aliexpress_login import update_login_progress as update_aliexpress_login_progress

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _to_out(db: Session, account: Account) -> AccountOut:
    count = db.execute(
        select(func.count()).select_from(Receipt).where(Receipt.account_id == account.id)
    ).scalar_one()
    last_collected_at = db.execute(
        select(func.max(CollectionRun.finished_at)).where(
            CollectionRun.account_id == account.id, CollectionRun.status == RunStatus.success
        )
    ).scalar_one()
    day_ago = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    failed_24h = db.execute(
        select(func.count()).select_from(CollectionRun).where(
            CollectionRun.account_id == account.id,
            CollectionRun.status == RunStatus.error,
            CollectionRun.started_at >= day_ago,
        )
    ).scalar_one()
    last_failed = db.execute(
        select(CollectionRun).where(
            CollectionRun.account_id == account.id,
            CollectionRun.status == RunStatus.error,
        ).order_by(CollectionRun.started_at.desc()).limit(1)
    ).scalars().first()
    last_run = db.execute(
        select(CollectionRun).where(
            CollectionRun.account_id == account.id,
            CollectionRun.status != RunStatus.running,
        ).order_by(CollectionRun.started_at.desc()).limit(1)
    ).scalars().first()

    out = AccountOut.model_validate(account)
    out.receipt_count = count
    out.last_collected_at = last_collected_at
    out.failed_runs_24h = failed_24h
    out.last_failed_at = last_failed.finished_at or last_failed.started_at if last_failed else None
    out.last_failed_error = last_failed.error_message if last_failed else None
    out.last_run_failed = last_run is not None and last_run.status == RunStatus.error
    adapter = get_adapter(account.store.code)
    out.store.capabilities = adapter.capabilities
    out.store.login_field = adapter.login_field
    out.store.store_class = store_classes.class_for_store(account.store)
    out.store.store_class_name = store_classes.label(out.store.store_class)
    return out


@router.get("", response_model=list[AccountOut])
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.execute(select(Account)).scalars().all()
    return [_to_out(db, a) for a in accounts]


@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    store = db.execute(select(Store).where(Store.code == payload.store_code)).scalar_one_or_none()
    if store is None:
        raise HTTPException(status_code=400, detail=f"Unknown store code: {payload.store_code}")

    account = Account(
        store_id=store.id,
        label=payload.label,
        phone_number=payload.phone_number,
        # "manual" stores (scanner/OCR) have nothing to log into — pending_otp
        # would just sit forever with no login UI that applies to it.
        status=AccountStatus.active if store.auth_type == AuthType.manual else AccountStatus.pending_otp,
    )
    db.add(account)
    db.flush()
    db.add(Schedule(account_id=account.id))
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(
    payload: AccountUpdate,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.delete("/{account_id}", status_code=204)
def delete_account(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    db.delete(account)
    db.commit()


@router.post("/{account_id}/login/start")
async def login_start(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    if not account.phone_number:
        raise HTTPException(status_code=400, detail="У акаунта не вказано номер телефону")
    cleared_stale_token = False
    if account.store.code == "dnipro-m" and account.credential is not None:
        # Do not let a stale empty/manual value keep the collector in a failure
        # loop while the user is starting a fresh SMS login. Preserve a valid
        # storage-state until the new login succeeds.
        try:
            old_token = decrypt(account.credential.access_token_enc) if account.credential.access_token_enc else ""
            parsed = json.loads(old_token) if old_token else None
            if not isinstance(parsed, dict) or not isinstance(parsed.get("cookies"), list):
                account.credential.access_token_enc = None
                cleared_stale_token = True
        except (TypeError, ValueError, json.JSONDecodeError):
            account.credential.access_token_enc = None
            cleared_stale_token = True
    if cleared_stale_token:
        # Persist the cleanup before contacting Dnipro-M. If SMS delivery is
        # rejected (CAPTCHA/rate limit), the collector still won't retry the
        # known-bad value on every scheduled run.
        db.commit()
    adapter = get_adapter(account.store.code)
    try:
        result = await adapter.start_login(account.phone_number)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upsert_credential(db, account, result)
    # A previous failed/manual attempt may have left an empty or malformed
    # Dnipro-M token. While waiting for the new SMS code it must not be used by
    # the collector, and it must not survive if confirmation fails.
    if account.store.code in {"dnipro-m", "fozzy"} and account.credential is not None:
        account.credential.access_token_enc = None
    account.status = AccountStatus.pending_otp
    db.commit()
    return {"pending": result.pending}


@router.post("/{account_id}/login/confirm")
async def login_confirm(
    payload: LoginConfirm,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    adapter = get_adapter(account.store.code)
    extra = dict(account.credential.extra) if account.credential else {}
    try:
        result = await adapter.confirm_login(extra, payload.code)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    return {"status": "active"}


@router.post("/{account_id}/login/card")
def login_card(
    payload: CardLogin,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """AuthType.card_number stores (see adapters/bars.py): no OTP round-trip,
    just the physical card number — activates the account in one call."""
    adapter = get_adapter(account.store.code)
    try:
        result = adapter.login_with_card(payload.card_number)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    return {"status": "active"}


@router.post("/{account_id}/login/device")
async def login_device(
    payload: DeviceUuidLogin,
    background: BackgroundTasks,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """Restore a METRO installation by UUID; cart/card scans never belong here."""
    adapter = get_adapter(account.store.code)
    try:
        result = await adapter.login_with_device_uuid(payload.device_uuid)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    run = start_collection(db, account, trigger="login")
    background.add_task(run_collection_in_background, run.id, account.id, trigger="login")
    return {
        "status": account.status.value,
        "collection_status": run.status.value,
        "receipts_found": run.receipts_found,
        "receipts_new": run.receipts_new,
    }


@router.post("/{account_id}/login/device-registration")
async def login_device_registration(
    payload: DeviceRegistrationLogin,
    background: BackgroundTasks,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    adapter = get_adapter(account.store.code)
    try:
        result = await adapter.login_with_device_registration(payload.cart_barcode, payload.card_barcode)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    run = start_collection(db, account, trigger="login")
    background.add_task(run_collection_in_background, run.id, account.id, trigger="login")
    return {
        "status": account.status.value,
        "collection_status": run.status.value,
        "receipts_found": run.receipts_found,
        "receipts_new": run.receipts_new,
    }


@router.post("/{account_id}/login/password")
async def login_password(
    payload: PasswordLogin,
    background: BackgroundTasks,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """Phone/login + password authentication.  Only the resulting encrypted
    session is persisted; the submitted password is never stored."""
    adapter = get_adapter(account.store.code)
    if not account.phone_number:
        missing = {"email": "email", "phone_or_email": "телефон або email"}.get(
            adapter.login_field, "номер телефону",
        )
        raise HTTPException(status_code=400, detail=f"У акаунта не вказано {missing}")
    if not payload.password:
        raise HTTPException(status_code=400, detail="Вкажіть пароль")
    try:
        result = await adapter.login_with_password(account.phone_number, payload.password)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upsert_credential(db, account, result)
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    run = start_collection(db, account, trigger="login")
    background.add_task(run_collection_in_background, run.id, account.id, trigger="login")
    return {
        "status": account.status.value,
        "collection_status": run.status.value,
        "receipts_found": run.receipts_found,
        "receipts_new": run.receipts_new,
    }


@router.get("/{account_id}/oauth/start")
def oauth_start(account: Account = Depends(get_account_or_404)):
    """For AuthType.oauth_browser stores (silpo): kicks off an OAuth 2.1 + PKCE
    login. Returns a URL for the frontend to open in a new tab; the provider
    redirects the user's browser to /api/oauth/{store_code}/callback (see
    app/api/oauth.py) once they finish logging in there."""
    adapter = get_adapter(account.store.code)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{settings.base_url}/api/oauth/{account.store.code}/callback"
    try:
        url = adapter.oauth_authorize_url(state=state, redirect_uri=redirect_uri, code_challenge=challenge)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    oauth_state.put(state, {"account_id": account.id, "verifier": verifier, "redirect_uri": redirect_uri})
    return {"authorize_url": url}


@router.post("/{account_id}/logout", response_model=AccountOut)
def logout(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    """Clears the stored credential so the account can be re-authenticated from
    scratch (SMS or manual token). Receipts already collected are kept."""
    if account.credential is not None:
        db.delete(account.credential)
    account.status = AccountStatus.pending_otp
    account.last_error = None
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.post("/{account_id}/reset-error", response_model=AccountOut)
def reset_error(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    """Clear a failed state while preserving the stored authorization."""
    account.last_error = None
    if account.status in {AccountStatus.needs_reauth, AccountStatus.error}:
        account.status = AccountStatus.active
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.post("/{account_id}/token", response_model=AccountOut)
def bootstrap_token(
    payload: TokenBootstrap,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """Manual escape hatch: paste in a token captured via mitmproxy, until real
    OTP automation exists for this store (see adapters/*.py docstrings)."""
    if account.store.code == "aliexpress":
        try:
            browser_state = json.loads(payload.access_token)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="AliExpress: storage state має бути коректним JSON") from exc
        if not isinstance(browser_state, dict) or not isinstance(browser_state.get("cookies"), list):
            raise HTTPException(status_code=400, detail="AliExpress: JSON має містити масив cookies")
    cred = account.credential
    if cred is None:
        cred = Credential(account=account)
        db.add(cred)
    cred.access_token_enc = encrypt(payload.access_token)
    cred.refresh_token_enc = encrypt(payload.refresh_token)
    cred.expires_at = payload.expires_at
    cred.extra = {**(cred.extra or {}), **payload.extra}
    account.status = AccountStatus.active
    account.last_error = None
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.put("/{account_id}/schedule")
def update_schedule(
    payload: ScheduleUpdate,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    schedule = account.schedule or Schedule(account_id=account.id)
    if payload.enabled is not None:
        schedule.enabled = payload.enabled
    if payload.cron_expression is not None:
        schedule.cron_expression = payload.cron_expression
    if account.schedule is None:
        db.add(schedule)
    db.commit()
    return {"status": "ok"}


@router.post("/{account_id}/collect", response_model=RunOut)
def collect_now(
    background: BackgroundTasks,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    if not get_adapter(account.store.code).capabilities["receipts"]:
        raise HTTPException(status_code=400, detail=f"{account.store.name}: автоматичний збір чеків не підтримується")
    run = start_collection(db, account, trigger="manual")
    background.add_task(
        run_collection_in_background, run.id, account.id, trigger="manual",
    )
    return run


@router.post("/{account_id}/collect-all", response_model=RunOut)
def collect_all(
    background: BackgroundTasks,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """Force a full history refresh for any source with receipt collection."""
    if not get_adapter(account.store.code).capabilities["receipts"]:
        raise HTTPException(status_code=400, detail="Повне завантаження для цього джерела не підтримується")
    run = start_collection(db, account, trigger="manual_all")
    background.add_task(
        run_collection_in_background,
        run.id,
        account.id,
        trigger="manual_all",
    )
    return run


@router.post("/{account_id}/import", response_model=RunOut)
async def import_receipts(
    body: ImportPayload,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    """Paste a raw API response captured via mitmproxy — for stores where live
    collection is blocked (e.g. АТБ, see RESEARCH.md)."""
    try:
        run = await import_for_account(db, account, body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return run


async def _finish_aliexpress_login(
    db: Session, account: Account, step: dict,
) -> AliLoginOut:
    if step.get("step") != "done":
        return AliLoginOut(**step)
    state = step.pop("storage_state")
    session_id = step.get("session_id")
    cred = account.credential
    if cred is None:
        # The collector runs immediately below in this same ORM session. Bind
        # through the relationship so it sees the newly-created credential
        # without requiring a new request/session.
        cred = Credential(account=account)
        db.add(cred)
    cred.access_token_enc = encrypt(state)
    cred.refresh_token_enc = None
    cred.expires_at = None
    account.status = AccountStatus.active
    account.last_error = None
    if account.schedule is not None:
        account.schedule.enabled = True
    db.commit()
    update_aliexpress_login_progress(
        session_id, "importing_orders", "Сесію збережено — завантажуємо замовлення",
    )
    run = await collect_for_account(db, account, trigger="login")
    if run.status != RunStatus.success:
        update_aliexpress_login_progress(
            session_id, "error", run.error_message or "AliExpress: імпорт не вдався",
        )
        raise HTTPException(status_code=400, detail=run.error_message or "AliExpress: імпорт не вдався")
    message = f"Вхід виконано. Знайдено {run.receipts_found}, нових {run.receipts_new}."
    update_aliexpress_login_progress(session_id, "done", message)
    return AliLoginOut(session_id=session_id, step="done", run=run, message=message)


@router.post("/{account_id}/aliexpress/login/start", response_model=AliLoginOut)
async def aliexpress_login_start(
    payload: AliLoginStart,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    if account.store.code != "aliexpress":
        raise HTTPException(status_code=400, detail="Цей вхід доступний лише для AliExpress")
    if not account.phone_number:
        raise HTTPException(status_code=400, detail="Вкажіть телефон або email в налаштуваннях акаунта")
    try:
        step = await start_aliexpress_login(
            account.id, account.phone_number, payload.password, payload.session_id,
        )
        return await _finish_aliexpress_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{account_id}/aliexpress/login/{session_id}/status", response_model=AliLoginOut)
async def aliexpress_login_status(
    session_id: str,
    account: Account = Depends(get_account_or_404),
):
    try:
        return AliLoginOut(**get_aliexpress_login_progress(account.id, session_id))
    except AdapterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{account_id}/aliexpress/login/{session_id}/screenshot")
async def aliexpress_login_screenshot(
    session_id: str,
    account: Account = Depends(get_account_or_404),
):
    try:
        image = await get_aliexpress_login_screenshot(account.id, session_id)
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except AdapterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/click", status_code=204)
async def aliexpress_login_click(
    session_id: str,
    payload: AliBrowserClick,
    account: Account = Depends(get_account_or_404),
):
    try:
        await control_aliexpress_login_click(account.id, session_id, payload.x, payload.y)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/pointer", status_code=204)
async def aliexpress_login_pointer(
    session_id: str,
    payload: AliBrowserPointer,
    account: Account = Depends(get_account_or_404),
):
    try:
        await control_aliexpress_login_pointer(
            account.id, session_id, payload.action, payload.x, payload.y,
        )
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/takeover", response_model=AliLoginOut)
async def aliexpress_login_takeover(
    session_id: str,
    account: Account = Depends(get_account_or_404),
):
    try:
        return AliLoginOut(**await request_aliexpress_manual_takeover(account.id, session_id))
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/key", status_code=204)
async def aliexpress_login_key(
    session_id: str,
    payload: AliBrowserKey,
    account: Account = Depends(get_account_or_404),
):
    try:
        await control_aliexpress_login_key(
            account.id, session_id, payload.key,
            alt=payload.alt, ctrl=payload.ctrl, meta=payload.meta, shift=payload.shift,
        )
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/text", status_code=204)
async def aliexpress_login_text(
    session_id: str,
    payload: AliBrowserText,
    account: Account = Depends(get_account_or_404),
):
    try:
        await control_aliexpress_login_text(account.id, session_id, payload.value)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/{session_id}/check", response_model=AliLoginOut)
async def aliexpress_login_check(
    session_id: str,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    try:
        step = await check_aliexpress_manual_login(account.id, session_id)
        return await _finish_aliexpress_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/otp", response_model=AliLoginOut)
async def aliexpress_login_otp(
    payload: AliLoginOtp,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    if account.store.code != "aliexpress":
        raise HTTPException(status_code=400, detail="Цей вхід доступний лише для AliExpress")
    try:
        step = await submit_aliexpress_otp(account.id, payload.session_id, payload.code)
        return await _finish_aliexpress_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/aliexpress/login/close", status_code=204)
async def aliexpress_login_close(
    payload: AliLoginClose,
    account: Account = Depends(get_account_or_404),
):
    if account.store.code == "aliexpress":
        await close_aliexpress_login(payload.session_id)


async def _finish_prom_login(db: Session, account: Account, step: dict) -> AliLoginOut:
    if step.get("step") != "done":
        return AliLoginOut(**step)
    state = step.pop("storage_state")
    session_id = step.get("session_id")
    cred = account.credential
    if cred is None:
        cred = Credential(account=account)
        db.add(cred)
    cred.access_token_enc = encrypt(state)
    cred.refresh_token_enc = None
    cred.expires_at = None
    account.status = AccountStatus.active
    account.last_error = None
    if account.schedule is not None:
        account.schedule.enabled = True
    db.commit()
    prom_login.update_login_progress(
        session_id, "importing_orders", "Сесію збережено — завантажуємо замовлення",
    )
    run = await collect_for_account(db, account, trigger="login")
    if run.status != RunStatus.success:
        prom_login.update_login_progress(
            session_id, "error", run.error_message or "Prom.ua: імпорт не вдався",
        )
        raise HTTPException(status_code=400, detail=run.error_message or "Prom.ua: імпорт не вдався")
    message = f"Вхід виконано. Знайдено {run.receipts_found}, нових {run.receipts_new}."
    prom_login.update_login_progress(session_id, "done", message)
    return AliLoginOut(session_id=session_id, step="done", run=run, message=message)


def _require_prom(account: Account) -> None:
    if account.store.code not in {"prom", "rozetka", "olx"}:
        raise HTTPException(status_code=400, detail="Цей вхід доступний лише для Prom.ua, Rozetka.ua або OLX")


@router.post("/{account_id}/prom/login/start", response_model=AliLoginOut)
@router.post("/{account_id}/rozetka/login/start", response_model=AliLoginOut)
@router.post("/{account_id}/olx/login/start", response_model=AliLoginOut)
async def prom_login_start(
    payload: PromLoginStart,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    _require_prom(account)
    if not account.phone_number:
        raise HTTPException(status_code=400, detail="Вкажіть номер телефону в налаштуваннях акаунта")
    try:
        step = await prom_login.start_login(
            account.id, account.phone_number, payload.session_id,
            site=account.store.code if account.store.code in {"rozetka", "olx"} else "prom",
        )
        return await _finish_prom_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{account_id}/prom/login/{session_id}/status", response_model=AliLoginOut)
@router.get("/{account_id}/rozetka/login/{session_id}/status", response_model=AliLoginOut)
@router.get("/{account_id}/olx/login/{session_id}/status", response_model=AliLoginOut)
async def prom_login_status(session_id: str, account: Account = Depends(get_account_or_404)):
    try:
        return AliLoginOut(**prom_login.get_login_progress(account.id, session_id))
    except AdapterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{account_id}/prom/login/{session_id}/screenshot")
@router.get("/{account_id}/rozetka/login/{session_id}/screenshot")
@router.get("/{account_id}/olx/login/{session_id}/screenshot")
async def prom_login_screenshot(session_id: str, account: Account = Depends(get_account_or_404)):
    try:
        image = await prom_login.get_login_screenshot(account.id, session_id)
        return Response(content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except AdapterError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/click", status_code=204)
@router.post("/{account_id}/rozetka/login/{session_id}/click", status_code=204)
@router.post("/{account_id}/olx/login/{session_id}/click", status_code=204)
async def prom_login_click(
    session_id: str, payload: AliBrowserClick, account: Account = Depends(get_account_or_404),
):
    try:
        await prom_login.control_login_click(account.id, session_id, payload.x, payload.y)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/pointer", status_code=204)
@router.post("/{account_id}/rozetka/login/{session_id}/pointer", status_code=204)
@router.post("/{account_id}/olx/login/{session_id}/pointer", status_code=204)
async def prom_login_pointer(
    session_id: str, payload: AliBrowserPointer, account: Account = Depends(get_account_or_404),
):
    try:
        await prom_login.control_login_pointer(
            account.id, session_id, payload.action, payload.x, payload.y,
        )
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/takeover", response_model=AliLoginOut)
@router.post("/{account_id}/rozetka/login/{session_id}/takeover", response_model=AliLoginOut)
@router.post("/{account_id}/olx/login/{session_id}/takeover", response_model=AliLoginOut)
async def prom_login_takeover(session_id: str, account: Account = Depends(get_account_or_404)):
    try:
        return AliLoginOut(**await prom_login.request_manual_takeover(account.id, session_id))
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/key", status_code=204)
@router.post("/{account_id}/rozetka/login/{session_id}/key", status_code=204)
@router.post("/{account_id}/olx/login/{session_id}/key", status_code=204)
async def prom_login_key(
    session_id: str, payload: AliBrowserKey, account: Account = Depends(get_account_or_404),
):
    try:
        await prom_login.control_login_key(
            account.id, session_id, payload.key,
            alt=payload.alt, ctrl=payload.ctrl, meta=payload.meta, shift=payload.shift,
        )
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/text", status_code=204)
@router.post("/{account_id}/rozetka/login/{session_id}/text", status_code=204)
@router.post("/{account_id}/olx/login/{session_id}/text", status_code=204)
async def prom_login_text(
    session_id: str, payload: AliBrowserText, account: Account = Depends(get_account_or_404),
):
    try:
        await prom_login.control_login_text(account.id, session_id, payload.value)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/{session_id}/check", response_model=AliLoginOut)
@router.post("/{account_id}/rozetka/login/{session_id}/check", response_model=AliLoginOut)
@router.post("/{account_id}/olx/login/{session_id}/check", response_model=AliLoginOut)
async def prom_login_check(
    session_id: str,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    try:
        step = await prom_login.check_manual_login(account.id, session_id)
        return await _finish_prom_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/otp", response_model=AliLoginOut)
@router.post("/{account_id}/rozetka/login/otp", response_model=AliLoginOut)
@router.post("/{account_id}/olx/login/otp", response_model=AliLoginOut)
async def prom_login_otp(
    payload: AliLoginOtp,
    account: Account = Depends(get_account_or_404),
    db: Session = Depends(get_db),
):
    _require_prom(account)
    try:
        step = await prom_login.submit_otp(account.id, payload.session_id, payload.code)
        return await _finish_prom_login(db, account, step)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{account_id}/prom/login/close", status_code=204)
@router.post("/{account_id}/rozetka/login/close", status_code=204)
@router.post("/{account_id}/olx/login/close", status_code=204)
async def prom_login_close(
    payload: AliLoginClose, account: Account = Depends(get_account_or_404),
):
    if account.store.code in {"prom", "rozetka", "olx"}:
        await prom_login.close_login(payload.session_id)


@router.get("/{account_id}/loyalty-code", response_model=LoyaltyCodeOut)
async def loyalty_code(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    """Fetched live on every call, not cached — some stores (АТБ) issue a
    short-lived rotating code. Not every store supports this (see adapters/*.py)."""
    if account.credential is None or not account.credential.access_token_enc:
        raise HTTPException(status_code=400, detail="Немає токена — спершу пройдіть авторизацію")
    adapter = get_adapter(account.store.code)
    try:
        access_token = await ensure_fresh_token(db, account, adapter)
        code = await adapter.get_loyalty_code(access_token, dict(account.credential.extra or {}))
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LoyaltyCodeOut(value=code.value, display=code.display, expires_at=code.expires_at)


@router.get("/{account_id}/fuel-prices", response_model=list[FuelPriceOut])
async def fuel_prices(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    """Fetched live, same as loyalty_code above — prices/personal discounts change
    during the day. Only fuel-station chains support this (see adapters/wog.py)."""
    if account.credential is None or not account.credential.access_token_enc:
        raise HTTPException(status_code=400, detail="Немає токена — спершу пройдіть авторизацію")
    adapter = get_adapter(account.store.code)
    try:
        access_token = await ensure_fresh_token(db, account, adapter)
        prices = await adapter.get_fuel_prices(access_token, dict(account.credential.extra or {}))
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persist_fuel_prices(db, account, prices)
    db.commit()
    return [
        FuelPriceOut(name=p.name, price=p.price, currency=p.currency, discount=p.discount, code=p.code)
        for p in prices
    ]


@router.get("/{account_id}/runs", response_model=list[RunOut])
def list_runs(account: Account = Depends(get_account_or_404), db: Session = Depends(get_db)):
    return db.execute(
        select(CollectionRun)
        .where(CollectionRun.account_id == account.id)
        .order_by(CollectionRun.started_at.desc())
        .limit(50)
    ).scalars().all()
