import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import dashboard
from app.db import Base
from app.models import models


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    store = models.Store(code="ali", name="AliExpress", auth_type=models.AuthType.oauth_browser)
    account = models.Account(store=store, label="Ali")
    db.add_all([store, account])
    db.flush()
    return db, account


def test_dashboard_totals_are_in_uah():
    db, account = _db()
    db.add_all([
        models.Receipt(
            account=account,
            store_code="ali",
            external_id="uah",
            purchased_at=dt.datetime(2026, 9, 1),
            total_amount=100,
            currency="UAH",
        ),
        models.Receipt(
            account=account,
            store_code="ali",
            external_id="usd",
            purchased_at=dt.datetime(2026, 9, 1),
            total_amount=10,
            currency="USD",
            exchange_rate_to_uah=40,
        ),
    ])
    db.commit()

    stats = dashboard.get_stats(db=db)
    by_store = dashboard.by_store(db=db)
    by_channel = dashboard.by_channel(db=db)
    trend = dashboard.timeseries(db=db, bucket="day")

    assert stats.total_spent == 500.0
    assert by_store[0].total_spent == 500.0
    assert by_channel[0].total_spent == 500.0
    assert trend[0].total_spent == 500.0


def test_dashboard_non_uah_without_rate_is_not_counted_as_uah():
    db, account = _db()
    db.add(models.Receipt(
        account=account,
        store_code="ali",
        external_id="usd-missing",
        purchased_at=dt.datetime(2026, 9, 1),
        total_amount=10,
        currency="USD",
        exchange_rate_to_uah=None,
    ))
    db.commit()

    assert dashboard.get_stats(db=db).total_spent == 0.0


def test_store_category_items_do_not_subtract_discount_twice():
    db, account = _db()
    category = models.Category(name="Food", slug="food", path="food", sort_order=0)
    receipt = models.Receipt(
        account=account,
        store_code="ali",
        external_id="discounted",
        purchased_at=dt.datetime(2026, 9, 1),
        total_amount=100,
        currency="UAH",
        total_discount=25,
    )
    receipt.items = [
        models.ReceiptItem(
            name="Discounted line",
            quantity=1,
            unit_price=100,
            total_price=100,
            discount=25,
            category=category,
        )
    ]
    db.add_all([category, receipt])
    db.commit()

    rows = dashboard.store_category_items(store_code="ali", category_id=category.id, db=db)

    assert rows[0].total_price == 100.0
    assert rows[0].discount == 25.0
    assert rows[0].net == 100.0
