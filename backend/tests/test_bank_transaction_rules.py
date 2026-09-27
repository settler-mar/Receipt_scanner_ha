import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import models
from app.services import bank_transaction_rules as rules_svc


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    bank = models.Store(code="monobank", name="Monobank", auth_type=models.AuthType.api_token)
    s.add(bank)
    s.flush()
    account = models.Account(store=bank, label="Monobank")
    s.add(account)
    s.commit()
    yield s
    s.close()


def _txn(db, description, amount, when):
    account = db.query(models.Account).one()
    txn = models.BankTransaction(
        account_id=account.id, store_code="monobank",
        external_id=f"{description}-{when.isoformat()}", amount=amount,
        occurred_at=when, description=description,
        status=models.BankTransactionStatus.unmatched,
    )
    db.add(txn)
    db.commit()
    return txn


def _zhenya_rule(db):
    rule = models.BankTransactionRule(
        label="Женя", counterparty_match="женя", priority=0,
        branches=[
            {"condition": {"kind": "count_in_month", "count": 1}, "ignore_reason": "utilities"},
            {"condition": {"kind": "position_in_month", "position": 1}, "ignore_reason": "rent"},
            {"condition": {"kind": "always"}, "ignore_reason": "utilities"},
        ],
    )
    db.add(rule)
    db.commit()
    return rule


def test_only_transfer_in_month_is_utilities(db):
    rule = _zhenya_rule(db)
    txn = _txn(db, "Женя П.", 3000, dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc))

    rules_svc.apply_rules(db)

    assert txn.ignore_reason == "utilities"
    assert txn.status == models.BankTransactionStatus.ignored
    assert txn.auto_rule_id == rule.id


def test_first_of_several_transfers_is_rent_rest_are_utilities(db):
    _zhenya_rule(db)
    first = _txn(db, "Женя П.", 8000, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    second = _txn(db, "Женя П.", 1500, dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc))
    third = _txn(db, "Женя П.", 1500, dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc))

    rules_svc.apply_rules(db)

    assert first.ignore_reason == "rent"
    assert second.ignore_reason == "utilities"
    assert third.ignore_reason == "utilities"


def test_a_second_transfer_arriving_later_reclassifies_the_first(db):
    _zhenya_rule(db)
    first = _txn(db, "Женя П.", 8000, dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    rules_svc.apply_rules(db)
    assert first.ignore_reason == "utilities"  # only one so far this month

    _txn(db, "Женя П.", 1500, dt.datetime(2026, 9, 20, tzinfo=dt.timezone.utc))
    rules_svc.apply_rules(db)

    assert first.ignore_reason == "rent"  # now the first of two


def test_manually_categorized_transaction_is_never_touched(db):
    _zhenya_rule(db)
    txn = _txn(db, "Женя П.", 3000, dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc))
    txn.ignore_reason = "pocket_money"
    db.commit()

    rules_svc.apply_rules(db)

    assert txn.ignore_reason == "pocket_money"
    assert txn.auto_rule_id is None


def test_non_matching_counterparty_is_untouched(db):
    _zhenya_rule(db)
    txn = _txn(db, "АТБ", 300, dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc))

    rules_svc.apply_rules(db)

    assert txn.ignore_reason is None
    assert txn.status == models.BankTransactionStatus.unmatched


def test_amount_based_condition(db):
    rule = models.BankTransactionRule(
        label="Оксана", counterparty_match="оксана", priority=0,
        branches=[
            {"condition": {"kind": "amount_gte", "amount": 5000}, "ignore_reason": "rent"},
            {"condition": {"kind": "always"}, "ignore_reason": "pocket_money"},
        ],
    )
    db.add(rule)
    db.commit()
    big = _txn(db, "Оксана Д.", 6000, dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc))
    small = _txn(db, "Оксана Д.", 200, dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc))

    rules_svc.apply_rules(db)

    assert big.ignore_reason == "rent"
    assert small.ignore_reason == "pocket_money"
