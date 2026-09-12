"""Recovering the shop behind a ДПС fiscal document (app/scanner/seller_lookup.py).

A ПРРО XML names the till, never the shop. These tests pin the two ways back to
it: the same till seen on a store-app receipt (exact shop + address + network),
and the seller's ПН (network only — one legal entity runs many shops).
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.base import NormalizedReceipt
from app.db import Base
from app.models.models import Receipt
from app.scanner import seller_lookup


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _app_receipt(db, *, rro_fiscal, shop_num, name, addr, store_code="atb"):
    """A receipt as the store's own app delivers it: shop named, till identified."""
    row = Receipt(
        account_id="acc", store_code=store_code, external_id=f"app:{rro_fiscal}",
        purchased_at=dt.datetime(2026, 8, 22, 14, 3, tzinfo=dt.timezone.utc),
        total_amount=100.0, shop_name=name, shop_address=addr,
        raw={"shop": {"num": shop_num, "name": name, "addr": addr,
                      "taxNum": "304872104175"},
             "rcpt": {"rroFiscal": rro_fiscal, "rroSerial": "КБ00003240"}},
    )
    db.add(row)
    db.commit()
    return row


def _fiscal_receipt(**over):
    data = dict(
        external_id="fiscal:3000802699:1010932",
        purchased_at=dt.datetime(2026, 9, 10, 14, 54, tzinfo=dt.timezone.utc),
        total_amount=515.8,
        raw={"method": "fiscal_check", "rro_serial": "КБ00003240",
             "seller_tax_number": "304872104175"},
    )
    data.update(over)
    return NormalizedReceipt(**data)


def test_same_till_gives_shop_address_and_network(db):
    source = _app_receipt(db, rro_fiscal="3000802699", shop_num="1363",
                          name="ПРОДУКТИ 1363",
                          addr="Україна, Київська обл., м.Святопетрівське, вул. Польова, 166")
    nr = _fiscal_receipt()
    hint = seller_lookup.enrich(db, nr)

    assert hint.matched_by == "rro"
    assert nr.shop_name == "ПРОДУКТИ 1363"
    assert nr.shop_address.endswith("вул. Польова, 166")
    assert nr.store_code == "atb"
    assert nr.raw["seller_match"]["store_number"] == "1363"
    assert nr.raw["seller_match"]["source_receipt_id"] == source.id


def test_another_till_of_the_same_chain_does_not_lend_its_address(db):
    """Same ПН, different shop: the network is safe to take, the address is not."""
    _app_receipt(db, rro_fiscal="3001125206", shop_num="855", name="ПРОДУКТИ 855",
                 addr="Україна, Київська обл., м.Київ, просп. Відрадний, 18")
    nr = _fiscal_receipt(raw={"method": "fiscal_check", "rro_serial": "КБ99999999",
                              "seller_tax_number": "304872104175"})
    hint = seller_lookup.enrich(db, nr)

    assert hint.matched_by == "network"
    assert nr.shop_name == "АТБ"
    assert nr.shop_address is None
    assert nr.store_code == "atb"


def test_only_fiscal_documents_are_enriched(db):
    _app_receipt(db, rro_fiscal="3000802699", shop_num="1363", name="ПРОДУКТИ 1363",
                 addr="вул. Польова, 166")
    nr = _fiscal_receipt(raw={"source": "manual"})

    assert seller_lookup.enrich(db, nr) is None
    assert nr.shop_name is None and nr.store_code is None
