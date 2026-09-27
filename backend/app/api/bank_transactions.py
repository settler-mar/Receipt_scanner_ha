from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.adapters.base import AdapterError
from app.adapters.registry import get_adapter
from app.core.security import decrypt
from app.db import get_db
from app.models.models import Account, BankTransaction, BankTransactionStatus, Category, Receipt
from app.schemas.schemas import (
    BankTransactionIgnore,
    BankTransactionLink,
    BankTransactionOut,
    BankTransactionPollIn,
    BankTransactionSummaryRow,
    BankTransactionUpdate,
    TokenTestResult,
)
from app.services import bank_transactions as bank_txn_service

router = APIRouter(prefix="/api/bank-transactions", tags=["bank-transactions"])


def _to_out(txn: BankTransaction) -> BankTransactionOut:
    out = BankTransactionOut.model_validate(txn, from_attributes=True)
    out.account_label = txn.account.label if txn.account else ""
    return out


@router.get("", response_model=list[BankTransactionOut])
def list_transactions(
    status: str | None = None,
    account_id: str | None = None,
    date_from: dt.datetime | None = None,
    date_to: dt.datetime | None = None,
    db: Session = Depends(get_db),
):
    query = (
        select(BankTransaction)
        .options(selectinload(BankTransaction.account))
        .order_by(BankTransaction.occurred_at.desc())
    )
    if status:
        try:
            query = query.where(BankTransaction.status == BankTransactionStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"unknown status {status!r}")
    if account_id:
        query = query.where(BankTransaction.account_id == account_id)
    if date_from:
        query = query.where(BankTransaction.occurred_at >= date_from)
    if date_to:
        query = query.where(BankTransaction.occurred_at <= date_to)
    return [_to_out(txn) for txn in db.execute(query).scalars().all()]


@router.get("/summary", response_model=list[BankTransactionSummaryRow])
def summary(db: Session = Depends(get_db)):
    """MCC-categorized totals for transactions that never got (and were never
    meant to get) a receipt — feeds the opt-in bank-spend view, kept separate
    from dashboard.py's receipt-based aggregations by construction. Pools
    every bank account together: the point is total non-receipt spend, not
    per-connection bookkeeping."""
    rows = db.execute(
        select(
            func.coalesce(BankTransaction.category_override_id, Category.id).label("category_id"),
            Category.name,
            func.sum(BankTransaction.amount),
            func.count(BankTransaction.id),
        )
        .outerjoin(Category, Category.id == BankTransaction.category_override_id)
        .where(
            BankTransaction.status.in_(
                [BankTransactionStatus.unmatched, BankTransactionStatus.ignored]
            )
        )
        .group_by(func.coalesce(BankTransaction.category_override_id, Category.id), Category.name)
    ).all()
    return [
        BankTransactionSummaryRow(
            category_id=category_id, category_name=name,
            total_amount=float(total), count=count,
        )
        for category_id, name, total, count in rows
    ]


def _get_account_or_404(account_id: str, db: Session) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Акаунт не знайдено")
    if not get_adapter(account.store.code).capabilities["transactions"]:
        raise HTTPException(status_code=400, detail=f"{account.store.name}: не є джерелом банківських транзакцій")
    return account


@router.post("/accounts/{account_id}/poll")
async def poll_account(
    account_id: str,
    background: BackgroundTasks,
    payload: BankTransactionPollIn | None = None,
    db: Session = Depends(get_db),
):
    account = _get_account_or_404(account_id, db)
    since = payload.since if payload else None
    if since is not None:
        # A wide backfill can mean many 31-day/60-second-throttled requests —
        # don't hold the request open for minutes, hand it to a background
        # task and let the journal (services/event_log) report completion.
        background.add_task(bank_txn_service.run_deep_sync_in_background, account_id, since)
        return {"started": True}
    try:
        return await bank_txn_service.collect_transactions_for_account(db, account)
    except AdapterError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/accounts/{account_id}/test-token", response_model=TokenTestResult)
async def test_account_token(account_id: str, db: Session = Depends(get_db)):
    account = _get_account_or_404(account_id, db)
    if account.credential is None or not account.credential.access_token_enc:
        raise HTTPException(status_code=400, detail="Спочатку вставте токен")
    adapter = get_adapter(account.store.code)
    if not adapter.capabilities["token_test"]:
        raise HTTPException(status_code=400, detail=f"{account.store.name}: перевірка токена не підтримується")
    result = await adapter.test_token(decrypt(account.credential.access_token_enc))
    return TokenTestResult(**result)


@router.post("/{transaction_id}/link", response_model=BankTransactionOut)
def link_transaction(transaction_id: str, payload: BankTransactionLink, db: Session = Depends(get_db)):
    txn = db.get(BankTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")
    if db.get(Receipt, payload.receipt_id) is None:
        raise HTTPException(status_code=404, detail="Чек не знайдено")
    txn.linked_receipt_id = payload.receipt_id
    txn.status = BankTransactionStatus.matched
    db.commit()
    return _to_out(txn)


@router.post("/{transaction_id}/unlink", response_model=BankTransactionOut)
def unlink_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = db.get(BankTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")
    txn.linked_receipt_id = None
    txn.status = BankTransactionStatus.unmatched
    db.commit()
    return _to_out(txn)


@router.post("/{transaction_id}/ignore", response_model=BankTransactionOut)
def ignore_transaction(transaction_id: str, payload: BankTransactionIgnore, db: Session = Depends(get_db)):
    txn = db.get(BankTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")
    txn.linked_receipt_id = None
    txn.status = BankTransactionStatus.ignored
    txn.ignore_reason = payload.reason
    db.commit()
    return _to_out(txn)


@router.patch("/{transaction_id}", response_model=BankTransactionOut)
def update_transaction(transaction_id: str, payload: BankTransactionUpdate, db: Session = Depends(get_db)):
    txn = db.get(BankTransaction, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Транзакцію не знайдено")
    txn.category_override_id = payload.category_override_id
    db.commit()
    return _to_out(txn)
