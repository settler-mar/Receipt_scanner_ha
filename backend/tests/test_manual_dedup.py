import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.api.inbox import _persist_receipt
from app.db import Base
from app.models.models import Account, AuthType, Receipt, ReceiptItem, Store
from app.services.manual_dedup import find_duplicate


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manual-dedup.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _normalized(external_id="scan:a"):
    return NormalizedReceipt(
        external_id=external_id,
        purchased_at=dt.datetime(2026, 8, 30, 8, 16, 56, tzinfo=dt.timezone.utc),
        total_amount=7061.43,
        shop_name="METRO",
        items=[NormalizedItem("Молоко", 100.0), NormalizedItem("Кава", 6961.43)],
    )


def test_repeated_manual_ingestion_returns_same_receipt(tmp_path):
    db = _db(tmp_path)
    scanner = Store(code="scanner", name="Сканер", auth_type=AuthType.manual)
    account = Account(store=scanner, label="Сканер")
    db.add(account); db.commit()

    first = _persist_receipt(db, account, _normalized("scan:first-photo"))
    second = _persist_receipt(db, account, _normalized("scan:second-photo"))

    assert second.id == first.id
    assert db.query(Receipt).count() == 1


def test_store_adapter_receipt_is_outside_manual_guard(tmp_path):
    db = _db(tmp_path)
    metro = Store(code="metro", name="METRO", auth_type=AuthType.device_uuid)
    account = Account(store=metro, label="METRO")
    db.add(account); db.flush()
    adapter_receipt = Receipt(
        account=account, store_code="metro", external_id="order:1",
        purchased_at=_normalized().purchased_at, total_amount=7061.43,
        currency="UAH", shop_name="METRO", raw={"source": "adapter"},
    )
    adapter_receipt.items = [ReceiptItem(name="Молоко", quantity=1, total_price=100.0)]
    db.add(adapter_receipt); db.commit()

    assert find_duplicate(db, _normalized()) is None


# The real OCR of a re-photographed receipt whose first parse came out garbled:
# the shop never resolved, the total was invented from one junk line, and the
# hour was misread (18:53 for 16:53). Only the transcript still describes it.
_GARBLED_OCR = (
    'ФОП СЕМЕРОГ ІННА ПЕТРІВНА\nМАГАЗИН "Магазин ФОП СЕМЕРОГ\nІННА ПЕТРІВНА"\n'
    "КИЇВСЬКА ОБЛАСТЬ, БУЧАНСЬКИЙ\nРАЙОН, СЕЛО СВЯТОПЕТРІВСЬКЕ\nВУЛ. БОЯРСЬКА\n"
    "Tf 3074818469\nКасир Касир\n\nЗ Надруковано: 06.09.2026,\n18:53:27\n\n"
    "Сік томатний Зл 270,00 €\nCik яблучний Зл 225,00 €\n\noe 0.554 x 250,00\n"
    "> Малина 138,50 Є\n\nБезготівкова, 793, 50 грн\nКартка\n\nona 633, 50 грн\n"
    "bea UB в 0,00\n\nlo сплати 633, 0 грн\n\nЧек i 4 7796620256\n"
    "06.09.2026 16:53:27\nОНЛАЙН\noH ПРРО 4001319262\nФІСКАЛЬНИЙ ЧЕК\n\nPoster ar\n"
)


def _garbled(db, account):
    """Persist the bad parse exactly as the pipeline stored it."""
    receipt = Receipt(
        account=account, store_code="scanner", external_id="scan:2b1d7557a48131f97814",
        purchased_at=dt.datetime(2026, 9, 6, 15, 53, 27, tzinfo=dt.timezone.utc),
        total_amount=250.0, currency="UAH", shop_name=None,
        raw={"method": "generic", "ingest_scope": "manual", "ocr_excerpt": _GARBLED_OCR},
    )
    receipt.items = [ReceiptItem(name="oe 0.554 x", quantity=1, total_price=250.0)]
    db.add(receipt); db.commit()
    return receipt


def _juice_receipt(external_id="scan:2cb1747158b6d8773cbe"):
    """The same paper, read correctly on a second photo — agreeing with the
    garbled row on nothing: not the total, the time, the shop, nor the items."""
    return NormalizedReceipt(
        external_id=external_id,
        purchased_at=dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc),
        total_amount=633.50,
        shop_name='МАГАЗИН "Магазин ФОП СЕМЕРОГ ІННА ПЕТРІВНА"',
        items=[NormalizedItem("Сік томатний 3л", 270.0),
               NormalizedItem("Сік яблучний 3л", 225.0),
               NormalizedItem("Малина", 138.50, quantity=0.554)],
    )


def test_good_rescan_matches_the_garbled_row_it_supersedes(tmp_path):
    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()
    garbled = _garbled(db, account)

    assert find_duplicate(db, _juice_receipt()) is garbled

    # And persisting it upgrades that row in place instead of adding a second.
    saved = _persist_receipt(db, account, _juice_receipt())
    assert saved.id == garbled.id
    assert db.query(Receipt).count() == 1
    assert saved.total_amount == 633.50
    assert len(saved.items) == 3
    assert saved.purchased_at.replace(tzinfo=dt.timezone.utc) == dt.datetime(
        2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc)


def test_another_receipt_from_the_same_shop_and_day_is_not_a_duplicate(tmp_path):
    """The guard above keys on the printed total appearing in the transcript, so
    a second genuine purchase at the same shop on the same day stays separate."""
    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()
    _garbled(db, account)

    vegetables = NormalizedReceipt(
        external_id="fiscal:4001319262:7734737664",
        purchased_at=dt.datetime(2026, 9, 6, 7, 12, 23, tzinfo=dt.timezone.utc),
        total_amount=698.25,
        shop_name='МАГАЗИН "Магазин ФОП СЕМЕРОГ ІННА ПЕТРІВНА"',
        items=[NormalizedItem("Картопля", 162.69, quantity=5.61),
               NormalizedItem("Морква", 38.28, quantity=1.32)],
    )
    assert find_duplicate(db, vegetables) is None


def test_three_photos_of_one_receipt_collapse_into_a_single_row(tmp_path):
    """The same paper shot three times: one photo the AI read with the shop, one
    where it resolved no shop, and a close-up whose QR decoded into a fiscal id.
    Nothing but the total and the printed second is shared by all three."""
    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()

    when = dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc)

    def photo(external_id, shop):
        return NormalizedReceipt(
            external_id=external_id, purchased_at=when, total_amount=633.50, shop_name=shop,
            items=[NormalizedItem("Сік томатний 3л", 270.0),
                   NormalizedItem("Сік яблучний 3л", 225.0),
                   NormalizedItem("Малина", 138.50, quantity=0.554)],
        )

    saved = [
        _persist_receipt(db, account, photo("scan:aaa", 'МАГАЗИН "ФОП СЕМЕРОГ ІННА ПЕТРІВНА"')),
        _persist_receipt(db, account, photo("scan:bbb", None)),
        _persist_receipt(db, account, photo("fiscal:4001319262:7738620256", None)),
    ]
    assert len({r.id for r in saved}) == 1
    assert db.query(Receipt).count() == 1


def test_same_total_at_a_different_second_still_needs_corroboration(tmp_path):
    """The shop-free shortcut keys on an exact timestamp; two genuine purchases
    of the same amount minutes apart must stay apart."""
    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()

    _persist_receipt(db, account, NormalizedReceipt(
        external_id="scan:first", total_amount=633.50, shop_name=None,
        purchased_at=dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc),
        items=[NormalizedItem("Кава", 633.50)]))
    second = NormalizedReceipt(
        external_id="scan:second", total_amount=633.50, shop_name=None,
        purchased_at=dt.datetime(2026, 9, 6, 13, 58, 5, tzinfo=dt.timezone.utc),
        items=[NormalizedItem("Чай", 633.50)])

    assert find_duplicate(db, second) is None
    _persist_receipt(db, account, second)
    assert db.query(Receipt).count() == 2


_QR_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=7738620256&date=20260906&time=165327&fn=4001319262&sm=633.50")


def test_a_filed_dps_document_is_found_through_the_scan_that_filed_it(tmp_path):
    """A receipt imported from XML carries no fiscal number of its own, but the
    scan the import was started from does — so a later photo of the same paper
    still resolves to it."""
    from app.models.scanner import ScanJob
    from app.scanner.fiscal_qr import parse_fiscal_url
    from app.services.manual_dedup import find_receipt_by_fiscal

    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()

    filed = _persist_receipt(db, account, NormalizedReceipt(
        external_id="cabinet-check:a6008d71", total_amount=633.50,
        purchased_at=dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc),
        shop_name="ФОП СЕМЕРОГ", items=[NormalizedItem("Малина", 633.50)]))
    db.add(ScanJob(receipt_id=filed.id, raw_ocr={"fiscal_url": _QR_URL}))
    db.commit()

    assert find_receipt_by_fiscal(db, parse_fiscal_url(_QR_URL)) is filed


def test_fiscal_lookup_is_exact_about_which_document_it_matches(tmp_path):
    from app.models.scanner import ScanJob
    from app.scanner.fiscal_qr import parse_fiscal_url
    from app.services.manual_dedup import find_receipt_by_fiscal

    db = _db(tmp_path)
    account = Account(store=Store(code="scanner", name="Сканер", auth_type=AuthType.manual),
                      label="Сканер")
    db.add(account); db.commit()
    filed = _persist_receipt(db, account, _normalized("fiscal:4001319262:7738620256"))
    db.add(ScanJob(receipt_id=filed.id, raw_ocr={"fiscal_url": _QR_URL}))
    db.commit()

    # A different document at the same registrar is a different purchase.
    other = _QR_URL.replace("id=7738620256", "id=7738620999")
    assert find_receipt_by_fiscal(db, parse_fiscal_url(other)) is None
    # A URL with no document number identifies nothing.
    assert find_receipt_by_fiscal(db, parse_fiscal_url(
        "https://cabinet.tax.gov.ua/cashregs/check?fn=4001319262")) is None


def test_money_compares_across_decimal_and_float():
    """The Numeric column reads back Decimal while parsers produce float, and
    `Decimal("7061.43") == 7061.43` is False — comparing them raw silently
    disables the whole guard."""
    from decimal import Decimal

    from app.services.manual_dedup import _same_amount

    assert _same_amount(Decimal("7061.43"), 7061.43)
    assert not _same_amount(Decimal("7061.43"), 7061.44)
    assert not _same_amount(None, 7061.43)


def test_ocr_transcript_match_needs_all_three_anchors(tmp_path):
    """Total, date and shop must all appear — any one alone is a coincidence."""
    from app.services.manual_dedup import _same_paper_by_text

    moment = dt.datetime(2026, 9, 6, 13, 53, 27, tzinfo=dt.timezone.utc)
    shop = "ФОП СЕМЕРОГ ІННА ПЕТРІВНА"
    assert _same_paper_by_text(_GARBLED_OCR, amount=633.50, moment=moment, shop=shop)
    # wrong total / wrong day / wrong shop each break it
    assert not _same_paper_by_text(_GARBLED_OCR, amount=999.99, moment=moment, shop=shop)
    assert not _same_paper_by_text(
        _GARBLED_OCR, amount=633.50,
        moment=dt.datetime(2026, 9, 7, 13, 53, 27, tzinfo=dt.timezone.utc), shop=shop)
    assert not _same_paper_by_text(_GARBLED_OCR, amount=633.50, moment=moment, shop="АТБ")
    # and an empty transcript can never anchor anything
    assert not _same_paper_by_text("", amount=633.50, moment=moment, shop=shop)
