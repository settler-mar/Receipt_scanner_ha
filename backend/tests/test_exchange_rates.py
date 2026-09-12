import datetime as dt
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.base import NormalizedReceipt
from app.db import Base
from app.models import models
from app.services.collector import _persist_receipts
from app.services.exchange_rates import NbuExchangeRateProvider


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def get(self, url, params):
        self.calls.append((url, params))
        return FakeResponse([{"cc": "USD", "rate": 41.2345}])


def test_uah_has_no_exchange_rate():
    provider = NbuExchangeRateProvider()
    assert provider.rate_to_uah("UAH", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)) is None


def test_nbu_rate_by_currency_and_purchase_date(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr("app.services.exchange_rates.httpx.Client", FakeClient)
    provider = NbuExchangeRateProvider()

    rate = provider.rate_to_uah("usd", dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc))

    assert rate == Decimal("41.2345")
    assert FakeClient.calls == [
        (
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange",
            {"valcode": "USD", "date": "20260901", "json": ""},
        )
    ]


def test_nbu_rate_is_cached(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr("app.services.exchange_rates.httpx.Client", FakeClient)
    provider = NbuExchangeRateProvider()

    provider.rate_to_uah("USD", dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))
    provider.rate_to_uah("USD", dt.datetime(2026, 9, 1, 22, tzinfo=dt.timezone.utc))

    assert len(FakeClient.calls) == 1


def test_collector_persists_exchange_rate(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    store = models.Store(code="aliexpress", name="AliExpress", auth_type=models.AuthType.oauth_browser)
    account = models.Account(store=store, label="Ali")
    db.add_all([store, account])
    db.commit()
    monkeypatch.setattr("app.services.collector.rate_to_uah", lambda currency, at: Decimal("41.2500"))

    _persist_receipts(db, account, [
        NormalizedReceipt(
            external_id="usd-1",
            purchased_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
            total_amount=10,
            currency="USD",
        )
    ])
    row = db.query(models.Receipt).one()

    assert float(row.exchange_rate_to_uah) == 41.25
