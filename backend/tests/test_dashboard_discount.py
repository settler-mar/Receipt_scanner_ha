import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import dashboard
from app.db import Base
from app.models import models


def _db_with_discounted_receipt():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()

    store = models.Store(code="atb", name="ATB", auth_type=models.AuthType.phone_otp)
    account = models.Account(store=store, label="ATB")
    category = models.Category(name="Food", slug="food", path="food", sort_order=0)
    receipt = models.Receipt(
        account=account,
        store_code="atb",
        external_id="r1",
        purchased_at=dt.datetime(2026, 9, 1),
        total_amount=80,  # paid; 20 was taken off by discounts
        total_discount=20,
        currency="UAH",
    )
    receipt.items = [
        models.ReceiptItem(name="Bread", quantity=1, total_price=80, discount=20, category=category)
    ]
    db.add_all([store, account, category, receipt])
    db.commit()
    return db


def test_receipt_totals_can_be_shown_before_discount():
    db = _db_with_discounted_receipt()

    assert dashboard.get_stats(db=db).total_spent == 80.0
    assert dashboard.get_stats(db=db, include_discount=False).total_spent == 100.0

    assert dashboard.by_store(db=db)[0].total_spent == 80.0
    assert dashboard.by_store(db=db, include_discount=False)[0].total_spent == 100.0

    assert dashboard.by_channel(db=db)[0].total_spent == 80.0
    assert dashboard.by_channel(db=db, include_discount=False)[0].total_spent == 100.0

    assert dashboard.timeseries(db=db, bucket="day")[0].total_spent == 80.0
    assert dashboard.timeseries(db=db, bucket="day", include_discount=False)[0].total_spent == 100.0


def test_item_totals_can_be_shown_before_discount():
    db = _db_with_discounted_receipt()

    food = [c for c in dashboard.by_category(db=db) if c.path == "food"][0]
    food_gross = [c for c in dashboard.by_category(db=db, include_discount=False) if c.path == "food"][0]
    assert (food.rollup_total, food_gross.rollup_total) == (80.0, 100.0)

    matrix = dashboard.store_category(db=db, include_discount=False)
    assert matrix.stores[0].total_spent == 100.0

    items = dashboard.store_category_items(store_code="atb", db=db, include_discount=False)
    assert (items[0].net, items[0].discount) == (100.0, 20.0)
    assert dashboard.store_category_items(store_code="atb", db=db)[0].net == 80.0
