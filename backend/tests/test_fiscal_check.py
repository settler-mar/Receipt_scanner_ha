"""Fiscal-check XML parsing + рознесення (app/scanner/fiscal_check.py)."""
from __future__ import annotations

import pathlib
import base64
import datetime as dt
import asyncio

import httpx
import pytest

from app.scanner import networks
from app.scanner.dedup import PurchaseEvidence, compare, from_fiscal_check
from app.scanner.fiscal_check import fetch_check, parse_check_xml
from app.scanner.reconcile import reconcile

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fiscal_check_ukrnafta.xml"

# The plain-text form the viewer returns alongside the XML (seller header only).
PLAINTEXT = (
    '                     АТ "УКРНАФТА"\n'
    "              АЗС №17/003 з магазином-кафе\n"
    "Рівненська область, Рівненський район, село Користь, вул. Київська, буд. 52\n"
    "                      ІД 00135390\n"
    "                    ПН 001353926654\n"
    "                      Касовий чек\n"
)


@pytest.fixture()
def check():
    return parse_check_xml(FIXTURE.read_bytes(), plaintext=PLAINTEXT,
                           raw_url="https://cabinet.tax.gov.ua/cashregs/check?id=20960071")


def test_header(check):
    assert check.fiscal_number == "3000878603"
    assert check.document_id == "20960071"
    assert check.rro_serial == "IS80021446"
    assert check.seller_edrpou == "00135390"
    assert check.seller_name == "АТ УКРНАФТА"
    assert "Користь" in (check.seller_address or "")
    assert check.issued_at is not None and check.issued_at.tzinfo is not None


def test_totals_and_vat(check):
    assert check.total == 1428.73
    assert check.vat_total == 238.12
    assert check.vat_rate == 20.0
    assert check.total_discount == 25.37


def test_lines_break_down_to_total(check):
    assert len(check.lines) == 2
    coffee, gas = check.lines

    assert coffee.name == "Американо з молоком"
    assert coffee.kind == "goods"
    assert coffee.gross == 74.0
    assert coffee.discount == 7.40
    assert coffee.net == 66.60

    assert gas.kind == "fuel"
    assert gas.uktzed == "2711190000"
    assert gas.excisable is True
    assert gas.quantity == 35.94
    assert gas.unit_price == 38.40
    assert gas.discount == 17.97
    assert gas.net == 1362.13

    assert check.net_total == 1428.73
    assert check.reconciles


def test_payment_and_reconcile_keys(check):
    pay = check.primary_payment()
    assert pay.method == "card"
    assert pay.payment_system == "VISA"
    assert pay.card_last4 == "1166"
    assert pay.rrn == "114021982107"
    assert pay.auth_code == "339342"

    keys = check.reconcile_keys()
    assert keys.total == 1428.73
    assert keys.card_last4 == "1166"
    assert keys.fuel_liters == 35.94
    assert "208367607" in keys.loyalty_ids


def test_fuel_summary(check):
    summary = check.fuel_summary()
    assert summary["liters"] == 35.94
    assert summary["price_per_liter"] == 38.40
    assert summary["amount"] == 1362.13


def test_allocate_shape(check):
    rows = check.allocate()
    assert [r["kind"] for r in rows] == ["goods", "fuel"]
    assert sum(r["net"] for r in rows) == 1428.73


def test_normalized_receipt_reconciles(check):
    receipt = check.to_normalized_receipt()
    assert receipt.external_id == "fiscal:3000878603:20960071"
    assert receipt.shop_name == "АТ УКРНАФТА"
    assert receipt.payment_type == "card"
    assert reconcile(receipt).ok
    assert receipt.raw["fuel"]["liters"] == 35.94


def test_brand_has_many_fiscal_identities(check):
    assert networks.by_edrpou("00135390").name == "Укрнафта"
    assert networks.by_edrpou("00152331").name == "Укрнафта"
    assert networks.identify('АЗК під вивіскою "UKRNAFTA"').name == "Укрнафта"


def test_official_token_api_is_preferred():
    xml = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chkAll")
        assert request.url.params["token"] == "open-token"
        assert request.url.params["type"] == "1"
        assert "captcha" not in request.url.params
        return httpx.Response(200, json={"check": base64.b64encode(xml).decode()})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_check(
                fiscal_number="3000878603", document_id="20960071",
                issued_at=dt.datetime(2026, 7, 5, 5, 39, tzinfo=dt.timezone.utc),
                total=1428.73, api_token="open-token", client=client,
            )

    result = asyncio.run(run())
    assert result is not None
    assert result.document_id == "20960071"


def test_cross_channel_dedup(check):
    fiscal = from_fiscal_check(check, brand="Укрнафта")
    bank = PurchaseEvidence(
        source="monobank", external_id="bank-42",
        occurred_at=check.issued_at + dt.timedelta(minutes=2),
        amount=1428.73, brand="Укрнафта", card_last4="1166",
    )
    result = compare(fiscal, bank)
    assert result.auto_merge
    assert "exact amount" in result.reasons


def test_amount_and_day_only_requires_review(check):
    fiscal = from_fiscal_check(check, brand="Укрнафта")
    bank = PurchaseEvidence(
        source="bank", external_id="bank-43",
        occurred_at=check.issued_at + dt.timedelta(hours=5),
        amount=1428.73,
    )
    assert compare(fiscal, bank).decision == "review"


# --------------------------------------------------------------------------- #
# ПРРО documents that arrive without the plain-text form: the seller header and
# the item barcodes have to come out of the XML itself.
# --------------------------------------------------------------------------- #
HEADER_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fiscal_check_prro_header.xml"


@pytest.fixture()
def xml_only_check():
    return parse_check_xml(HEADER_FIXTURE.read_bytes())


def test_seller_header_from_xml_rows(xml_only_check):
    check = xml_only_check
    # "<точка> Прізвище І. Б. <код>" is the operator line: it must not be sold
    # to the user as the shop's name.
    assert check.cashier == "Приватний П. П."
    assert check.seller_name is None
    assert check.store_number == "00003"
    assert check.register == "3"
    assert check.shift == "2242"
    assert check.seller_tax_number == "300000000001"
    assert check.seller_edrpou is None          # 12-digit ПН is not a ЄДРПОУ


def test_item_barcodes_from_xml(xml_only_check):
    assert [l.barcode for l in xml_only_check.lines] == ["4823086123236", "4820075504250"]
    receipt = xml_only_check.to_normalized_receipt()
    assert receipt.items[0].barcode == "4823086123236"
    assert receipt.raw["seller_tax_number"] == "300000000001"


def test_known_tax_number_names_the_retail_brand():
    """ПРРО XML has no shop sign — the seller ПН is the only way to know it."""
    xml = HEADER_FIXTURE.read_bytes().replace(b"300000000001", b"304872104175")
    check = parse_check_xml(xml)
    assert check.seller_name == "АТБ"
    assert check.seller_edrpou is None      # ЄДРПОУ is not derivable from ПН
    assert check.cashier == "Приватний П. П."
