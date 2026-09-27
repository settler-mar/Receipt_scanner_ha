import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.base import NormalizedBankTransaction
from app.db import Base
from app.models import models
from app.services import bank_transactions
from app.services import monobank as mono_client


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    shop = models.Store(code="atb", name="АТБ", auth_type=models.AuthType.phone_otp)
    bank = models.Store(code="monobank", name="Monobank", auth_type=models.AuthType.api_token)
    s.add_all([shop, bank])
    s.flush()
    shop_account = models.Account(store=shop, label="АТБ")
    bank_account = models.Account(store=bank, label="Monobank — картка 1")
    s.add_all([shop_account, bank_account])
    s.commit()
    yield s
    s.close()


def _bank_account(db):
    return db.query(models.Account).join(models.Store).filter(models.Store.code == "monobank").one()


def _receipt(db, total, when, shop_name="АТБ-Маркет"):
    account = db.query(models.Account).join(models.Store).filter(models.Store.code == "atb").one()
    r = models.Receipt(
        account_id=account.id, store_code="atb", external_id=f"ext-{when.isoformat()}-{total}",
        purchased_at=when, total_amount=total, shop_name=shop_name,
    )
    db.add(r)
    db.commit()
    return r


def _txn(db, external_id, amount, when, description=None):
    txn = models.BankTransaction(
        account_id=_bank_account(db).id, store_code="monobank", external_id=external_id,
        amount=amount, occurred_at=when, description=description,
        status=models.BankTransactionStatus.unmatched,
    )
    db.add(txn)
    db.commit()
    return txn


def test_match_transaction_exact_single_candidate(db):
    when = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
    receipt = _receipt(db, 150.00, when)
    txn = _txn(db, "m1", 150.00, when + dt.timedelta(minutes=5))

    bank_transactions.match_transaction(db, txn)

    assert txn.status == models.BankTransactionStatus.matched
    assert txn.linked_receipt_id == receipt.id


def test_match_transaction_no_candidate_stays_unmatched(db):
    when = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
    _receipt(db, 150.00, when)
    txn = _txn(db, "m2", 999.00, when)

    bank_transactions.match_transaction(db, txn)

    assert txn.status == models.BankTransactionStatus.unmatched
    assert txn.linked_receipt_id is None


def test_match_transaction_ambiguous_needs_review(db):
    when = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
    _receipt(db, 150.00, when, shop_name="АТБ-Маркет №1")
    _receipt(db, 150.00, when + dt.timedelta(minutes=10), shop_name="АТБ-Маркет №2")
    txn = _txn(db, "m3", 150.00, when, description="ЩОСЬ ІНШЕ")

    bank_transactions.match_transaction(db, txn)

    assert txn.status == models.BankTransactionStatus.needs_review
    assert txn.linked_receipt_id is None


def test_ingest_transactions_is_idempotent(db):
    account = _bank_account(db)
    item = NormalizedBankTransaction(
        external_id="dup-1", occurred_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        amount=-150.00, mcc=5411, description="АТБ",
    )
    created_first = bank_transactions.ingest_transactions(db, account, [item])
    created_second = bank_transactions.ingest_transactions(db, account, [item])

    assert created_first == 1
    assert created_second == 0
    assert db.query(models.BankTransaction).count() == 1


def test_ingest_transactions_flags_incoming_transfer_as_ignored(db):
    account = _bank_account(db)
    item = NormalizedBankTransaction(
        external_id="in-1", occurred_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        amount=200.00, mcc=6538, description="Переказ",
    )
    bank_transactions.ingest_transactions(db, account, [item])
    txn = db.query(models.BankTransaction).one()

    assert txn.status == models.BankTransactionStatus.ignored
    assert txn.ignore_reason == "incoming_transfer"


def test_ingest_transactions_scoped_per_account(db):
    """Two bank accounts (e.g. two personal Monobank tokens) each get their own
    row for the same external_id — the unique key is (account, external_id),
    not external_id alone."""
    account_a = _bank_account(db)
    account_b = models.Account(store_id=account_a.store_id, label="Monobank — картка 2")
    db.add(account_b)
    db.commit()

    item = NormalizedBankTransaction(
        external_id="shared-id", occurred_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
        amount=-50.00,
    )
    bank_transactions.ingest_transactions(db, account_a, [item])
    bank_transactions.ingest_transactions(db, account_b, [item])

    assert db.query(models.BankTransaction).count() == 2


def test_chunk_count_single_request_for_short_range():
    since = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc)
    assert mono_client.chunk_count(since, until) == 1


def test_chunk_count_splits_wide_range_into_31_day_windows():
    since = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    until = dt.datetime(2026, 9, 27, tzinfo=dt.timezone.utc)  # ~269 days
    assert mono_client.chunk_count(since, until) == 9
