"""Receipt-recognition pipeline units (ROADMAP.md Крок 3)."""
from __future__ import annotations

import asyncio
import datetime as dt
import io

import pytest

from app.scanner import networks
from app.scanner.aspect import (
    ImageShape,
    color_stats,
    image_dimensions,
    route,
    route_by_shape,
)
from app.scanner.fiscal_qr import parse_fiscal_url
from app.scanner.generic import parse_generic
from app.scanner.reconcile import reconcile
from app.scanner.templates import identify_template, load_templates, parse_with_template
from app.scanner.pipeline import scan_receipt_bytes


def _png(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# aspect routing
# --------------------------------------------------------------------------- #
def test_image_dimensions_roundtrip():
    shape = image_dimensions(_png(400, 1200))
    assert (shape.width, shape.height) == (400, 1200)
    assert shape.ratio == 3.0


@pytest.mark.parametrize("w,h,expected", [
    (400, 1200, "receipt"),
    (500, 1100, "receipt"),
    (1000, 1400, "document"),
    (1200, 800, "document"),
    (800, 800, "document"),
])
def test_route_by_shape(w, h, expected):
    assert route_by_shape(ImageShape(w, h)) == expected


def test_route_threshold_is_configurable():
    assert route_by_shape(ImageShape(1000, 1600), receipt_ratio=1.5) == "receipt"


def _solid(w, h, rgb) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _noisy(w, h) -> bytes:
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(1)
    arr = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    arr[:, :, 0] = 230
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def test_color_stats_monochrome_vs_colour():
    grey = color_stats(_solid(300, 300, (180, 180, 180)))
    assert grey.is_monochrome()
    colour = color_stats(_noisy(300, 300))
    assert not colour.is_monochrome()


def test_route_monochrome_wins_over_shape():
    grey = color_stats(_solid(1600, 900, (170, 170, 170)))
    assert route(ImageShape(1600, 900), grey) == "receipt"


def test_route_colour_page_is_document():
    colour = color_stats(_noisy(1600, 900))
    assert route(ImageShape(1600, 900), colour) == "document"


def test_route_without_colour_signal_uses_shape():
    assert route(ImageShape(400, 1400), None) == "receipt"
    assert route(ImageShape(1600, 900), None) == "document"


# --------------------------------------------------------------------------- #
# ЄДРПОУ → chain reference
# --------------------------------------------------------------------------- #
def test_networks_identify_by_edrpou_in_text():
    ref = networks.identify("ТОВ \"АТБ-МАРКЕТ\"\nЄ Д Р П О У  30487219\nчек")
    assert ref is not None and ref.name == "АТБ" and ref.store_code == "atb"


def test_networks_identify_none_when_absent():
    assert networks.identify("random receipt with code 12345") is None


# --------------------------------------------------------------------------- #
# fiscal QR parsing
# --------------------------------------------------------------------------- #
def test_parse_fiscal_url_full():
    url = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=95123&date=20260830&time=194512&fn=4000123456&sum=347.20")
    qr = parse_fiscal_url(url)
    assert qr is not None
    assert qr.fiscal_number == "4000123456"
    assert qr.document_id == "95123"
    assert qr.total_amount == 347.20
    assert qr.issued_at is not None and qr.issued_at.tzinfo is not None
    assert qr.dedup_key == "fiscal:4000123456:95123"


def test_parse_fiscal_url_rejects_non_tax_host():
    assert parse_fiscal_url("https://example.com/check?fn=1&id=2") is None
    assert parse_fiscal_url("not a url at all") is None


def test_parse_fiscal_url_alt_param_names():
    qr = parse_fiscal_url("https://cabinet.tax.gov.ua/check?fn=1&id=2&sm=10,50&date=30.08.2026")
    assert qr is not None and qr.total_amount == 10.5


_QR_URL = ("https://cabinet.tax.gov.ua/cashregs/check"
           "?id=7738620256&date=20260906&time=165327&fn=4001319262&sm=633.50")


def _photo_with_qr(url: str = _QR_URL, *, qr_px: int = 150, washed_out: bool = True) -> bytes:
    """A tall receipt-shaped frame with a small, low-contrast QR in its footer —
    the shape of an actual phone photo rather than a clean rendering."""
    import cv2
    import numpy as np

    qr = cv2.QRCodeEncoder.create().encode(url)
    qr = cv2.resize(qr, (qr_px, qr_px), interpolation=cv2.INTER_NEAREST)
    if washed_out:  # thermal paper under room light: grey-on-grey, not black-on-white
        qr = np.clip(qr.astype(np.int16) * 0.55 + 70, 0, 255).astype(np.uint8)

    page = np.full((2400, 1350), 205, np.uint8)
    top = 2400 - 350
    page[top:top + qr_px, 600:600 + qr_px] = qr
    page = cv2.GaussianBlur(page, (3, 3), 0)
    noise = np.random.default_rng(0).normal(0, 6, page.shape)
    page = np.clip(page.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return cv2.imencode(".jpg", page, [int(cv2.IMWRITE_JPEG_QUALITY), 80])[1].tobytes()


def test_qr_decoding_survives_a_small_low_contrast_photo():
    """The plain one-shot decode opencv offers fails on this frame; the variant
    ladder in scan_qr is what makes a real phone photo readable."""
    import cv2
    import numpy as np

    from app.scanner.fiscal_qr import QR_DECODED, decode_fiscal_qr, scan_qr

    image = _photo_with_qr()
    arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert not cv2.QRCodeDetector().detectAndDecode(arr)[0], "plain decode unexpectedly worked"

    result = scan_qr(image)
    assert result.state == QR_DECODED
    assert result.payloads == [_QR_URL]
    assert decode_fiscal_qr(image).document_id == "7738620256"


def test_zxing_reads_the_untouched_frame_before_opencv_or_preprocessing():
    """zxing-cpp is both faster and stronger than cv2.QRCodeDetector — it reads
    dense codes on crumpled thermal paper that opencv misses entirely — so a
    readable photo must never pay for preprocessing or the weaker engine."""
    from app.scanner import fiscal_qr as fq
    from app.scanner.qr import decoders

    opencv_calls = []

    def spy(detector, arr):
        opencv_calls.append(arr.shape)
        return []

    original, decoders._decode_one = decoders._decode_one, spy
    try:
        result = fq.scan_qr(_photo_with_qr())
    finally:
        decoders._decode_one = original

    assert result.state == fq.QR_DECODED
    assert result.payloads == [_QR_URL]
    assert opencv_calls == [], "opencv should not have been reached at all"


def test_qr_decoding_degrades_to_opencv_without_zxing(monkeypatch):
    """zxing-cpp is optional so a platform with no wheel still scans receipts."""
    from app.scanner import fiscal_qr as fq
    from app.scanner.qr import decoders

    monkeypatch.setattr(decoders, "_decode_zxing", lambda _arr: [])
    result = fq.scan_qr(_photo_with_qr())
    assert result.state == fq.QR_DECODED
    assert result.payloads == [_QR_URL]


def test_qr_too_degraded_to_read_is_reported_as_unreadable_not_absent():
    """A QR the detector can see but not decode must not be reported the same as
    a receipt with no QR — the user needs to be told to re-shoot it closer.

    Real photos land here by being too far away; the finder squares survive long
    after the data modules stop resolving, so scrambling only the interior
    reproduces that state deterministically.
    """
    import cv2
    import numpy as np

    from app.scanner.fiscal_qr import QR_NONE, QR_UNREADABLE, scan_qr

    qr = cv2.resize(cv2.QRCodeEncoder.create().encode(_QR_URL), (300, 300),
                    interpolation=cv2.INTER_NEAREST)
    rng = np.random.default_rng(1)
    qr[90:210, 90:210] = rng.integers(0, 2, (120, 120), dtype=np.uint8) * 255

    page = np.full((1400, 700), 235, np.uint8)
    page[1000:1300, 200:500] = qr
    image = cv2.imencode(".png", page)[1].tobytes()

    degraded = scan_qr(image)
    assert degraded.state == QR_UNREADABLE
    assert degraded.payloads == []

    assert scan_qr(_png(400, 1400)).state == QR_NONE


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #
def test_reconcile_balanced():
    from app.adapters.base import NormalizedItem, NormalizedReceipt

    r = NormalizedReceipt(
        external_id="x", purchased_at=None, total_amount=100.0,
        items=[NormalizedItem("a", 60.0), NormalizedItem("b", 40.0)],
    )
    assert reconcile(r).ok


def test_reconcile_off():
    from app.adapters.base import NormalizedItem, NormalizedReceipt

    r = NormalizedReceipt(
        external_id="x", purchased_at=None, total_amount=100.0,
        items=[NormalizedItem("a", 60.0)],
    )
    rec = reconcile(r)
    assert not rec.ok and rec.delta == -40.0


def test_reconcile_no_items():
    from app.adapters.base import NormalizedReceipt

    assert not reconcile(NormalizedReceipt(external_id="x", purchased_at=None, total_amount=50.0)).ok


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def test_templates_load_and_validate():
    specs = load_templates()
    assert any(s.code == "atb" for s in specs)


def test_atb_template_identifies_and_parses():
    text = "\n".join([
        'ТОВ "АТБ-МАРКЕТ"',
        "ЄДРПОУ 30487219",
        "30.08.2026 19:45",
        "Молоко Яготинське 2,6% 1 x 42.90 42.90 А",
        "Хліб Кулиничі 2 x 21.00 42.00 А",
        "Знижка 5.00",
        "СУМА ДО СПЛАТИ 79.90",
        "БЕЗГОТІВКОВА 79.90",
    ])
    spec = identify_template(text)
    assert spec is not None and spec.code == "atb"
    receipt = parse_with_template(spec, text)
    assert receipt.shop_name == "АТБ"
    assert len(receipt.items) == 2
    assert receipt.total_amount == 79.90
    # second item had a 5.00 discount line applied under it
    assert receipt.items[1].discount == 5.0
    assert reconcile(receipt).ok


def test_unknown_layout_has_no_template():
    assert identify_template("Just some random text\nwith no markers") is None


# --------------------------------------------------------------------------- #
# generic heuristic parser
# --------------------------------------------------------------------------- #
def test_generic_parser_extracts_items_and_total():
    text = "\n".join([
        "МАГАЗИН БІЛЯ ДОМУ",
        "01.02.2026 10:15",
        "Банани 1.230 x 34.90 42.93",
        "Вода негазована 12.00",
        "Сума 54.93",
        "Дякуємо за покупку",
    ])
    r = parse_generic(text)
    assert r is not None
    assert r.total_amount == 54.93
    names = {it.name for it in r.items}
    assert "Банани" in names and "Вода негазована" in names
    assert reconcile(r).ok


def test_generic_parser_returns_none_on_junk():
    assert parse_generic("!!!\n???\n...") is None


def test_a_qr_naming_an_already_filed_document_stops_before_the_model(monkeypatch):
    """The QR names the ДПС document, so once that document is on file another
    photo of it settles the question outright — neither a ДПС fetch nor a vision
    call could improve on the receipt already there, so neither should run."""
    from app.scanner.fiscal_qr import QR_DECODED, QrScan

    url = "https://cabinet.tax.gov.ua/cashregs/check?id=7738620256&fn=4001319262&sm=633.50"
    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_DECODED, [url]))

    async def ai_must_not_run():
        raise AssertionError("vision was called for a receipt already on file")

    async def cabinet_must_not_run(*_args, **_kwargs):
        raise AssertionError("ДПС was queried for a receipt already on file")

    monkeypatch.setattr("app.scanner.pipeline.tax_cabinet.fetch_structured", cabinet_must_not_run)

    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 1400), ai_ocr=ai_must_not_run,
        known_receipt=lambda fiscal: "receipt-42" if fiscal.document_id == "7738620256" else None,
    ))

    assert outcome.duplicate_receipt_id == "receipt-42"
    assert "already filed as receipt receipt-42" in " ".join(outcome.notes)


def test_an_unfiled_qr_still_runs_the_whole_pipeline(monkeypatch):
    """The short-circuit above must not fire for a document not yet on file."""
    from app.scanner.fiscal_qr import QR_DECODED, QrScan

    url = "https://cabinet.tax.gov.ua/cashregs/check?id=111&fn=222&sm=10.00"
    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_DECODED, [url]))

    async def vision():
        return {"shop_name": "МАГАЗИН", "total_amount": 10.0,
                "items": [{"name": "Хліб", "quantity": 1,
                           "unit_price": 10.0, "total_price": 10.0}]}

    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 1400), ai_ocr=vision, enable_tax_cabinet=False,
        known_receipt=lambda _fiscal: None,
    ))
    assert outcome.duplicate_receipt_id is None
    assert outcome.receipt is not None


def test_the_image_qr_is_read_even_when_a_known_one_is_supplied(monkeypatch):
    """A caller-supplied QR is a fallback, never a reason to skip the image's
    own: the QR is the authoritative source, so every run retries it first —
    and `qr_state` has to keep describing this photo, not the caller's hint."""
    from app.scanner.fiscal_qr import QR_DECODED, FiscalQr, QrScan

    on_the_photo = ("https://cabinet.tax.gov.ua/cashregs/check"
                    "?id=111&fn=222&sm=10.00&date=20260906")
    monkeypatch.setattr("app.scanner.pipeline.scan_qr",
                        lambda _image: QrScan(QR_DECODED, [on_the_photo]))

    stale = FiscalQr(raw_url="https://cabinet.tax.gov.ua/cashregs/check?id=999&fn=888",
                     fiscal_number="888", document_id="999")
    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 1400), enable_tax_cabinet=False, fiscal_override=stale))

    assert outcome.fiscal.document_id == "111", "the photo's own QR must win"
    assert outcome.qr_state == QR_DECODED


def test_a_known_qr_is_used_when_this_image_yields_none(monkeypatch):
    """Decoding sits right at the edge of what the decoder manages, so a URL an
    earlier pass recovered is a fact about the receipt worth keeping — a rerun
    can then only gain on the last attempt."""
    from app.scanner.fiscal_qr import QR_UNREADABLE, FiscalQr, QrScan

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_UNREADABLE))

    known = FiscalQr(raw_url="https://cabinet.tax.gov.ua/cashregs/check?id=999&fn=888",
                     fiscal_number="888", document_id="999", total_amount=633.50)
    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 1400), enable_tax_cabinet=False, fiscal_override=known))

    assert outcome.fiscal is known
    # The state still reports what this photo actually showed.
    assert outcome.qr_state == QR_UNREADABLE
    assert "reusing a previously decoded fiscal QR" in " ".join(outcome.notes)


def test_unreadable_qr_outranks_a_reconciled_ai_parse(monkeypatch):
    """The fiscal QR is the authoritative source — it yields the ДПС document
    itself. When one is visibly present but unread, a vision guess must not file
    itself unattended, however neatly it balances: the user re-shoots the QR."""
    from app.scanner.fiscal_qr import QR_UNREADABLE, QrScan

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_UNREADABLE))

    async def confident_ai():
        return {"shop_name": "МАГАЗИН", "total_amount": 100.0,
                "items": [{"name": "Хліб", "quantity": 1,
                           "unit_price": 100.0, "total_price": 100.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=confident_ai))
    assert outcome.reconciliation.ok, "the parse itself balances"
    assert outcome.confidence == "low", "but it must not auto-save"
    assert not outcome.ok
    assert "re-shoot the QR" in " ".join(outcome.notes)


def test_a_decoded_qr_still_lets_a_good_parse_through(monkeypatch):
    """The guard above must not fire when the QR was actually read."""
    from app.scanner.fiscal_qr import QR_NONE, QrScan

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(QR_NONE))

    async def confident_ai():
        # Two lines that add up to the printed total: a real cross-check, unlike
        # a single line that equals the total by construction.
        return {"shop_name": "МАГАЗИН", "total_amount": 100.0,
                "items": [{"name": "Хліб", "quantity": 1,
                           "unit_price": 60.0, "total_price": 60.0},
                          {"name": "Молоко", "quantity": 1,
                           "unit_price": 40.0, "total_price": 40.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=confident_ai))
    assert outcome.confidence == "high"


def test_total_summed_from_items_never_counts_as_reconciled():
    """Guards the failure that let a garbled OCR auto-save: with no printed
    total the parser sums the items, after which the balance check compares that
    sum against itself and always passes. Such a draft must go to review."""
    text = "\n".join([
        "МАГАЗИН",
        "01.02.2026 10:15",
        "ое 0.554 x 250,00",   # one mangled line, no "Сума" anywhere
    ])
    r = parse_generic(text)
    assert r is not None
    assert r.total_amount == 250.0
    assert r.raw["total_inferred"] is True

    check = reconcile(r)
    assert not check.ok
    assert "summed from items" in check.reason


# --------------------------------------------------------------------------- #
# pipeline orchestration
# --------------------------------------------------------------------------- #
def test_pipeline_falls_through_to_ai_when_no_qr_no_ocr():
    ai_payload = {
        "shop_name": "Тестмаркет", "purchased_at": "2026-08-30T12:00:00",
        "total_amount": 25.0, "currency": "UAH",
        "items": [{"name": "Товар", "quantity": 1, "unit_price": 15.0, "total_price": 15.0},
                  {"name": "Ще товар", "quantity": 1, "unit_price": 10.0, "total_price": 10.0}],
    }

    async def fake_ai():
        return ai_payload

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=fake_ai))
    assert outcome.method == "ai"
    assert outcome.confidence == "high"
    assert outcome.receipt is not None
    assert outcome.receipt.external_id.startswith("scan:")
    assert outcome.receipt.shop_name == "Тестмаркет"


def test_fiscal_qr_metadata_overrides_ai_guesses(monkeypatch):
    from app.scanner.fiscal_qr import QR_DECODED, FiscalQr, QrScan

    fiscal = FiscalQr(
        raw_url=("https://cabinet.tax.gov.ua/cashregs/check?date=20260711&time=1135"
                 "&id=10430113&sm=747.84&fn=3001101692"),
        fiscal_number="3001101692", document_id="10430113",
        issued_at=dt.datetime(2026, 7, 11, 8, 35, tzinfo=dt.timezone.utc),
        total_amount=747.84,
    )
    monkeypatch.setattr("app.scanner.pipeline.scan_qr",
                        lambda _image: QrScan(QR_DECODED, [fiscal.raw_url]))

    async def fake_ai():
        return {
            "shop_name": "AZK 17018, BEREZHANY",
            "purchased_at": "2026-09-01T12:00:00",
            "total_amount": 700,
            "items": [{"name": "Американо з молоком, XL", "quantity": 1,
                       "unit_price": 86, "total_price": 86}],
        }

    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 800), ai_ocr=fake_ai, enable_tax_cabinet=False,
    ))
    assert outcome.receipt is not None
    assert outcome.receipt.shop_name == "AZK 17018, BEREZHANY"
    assert outcome.receipt.purchased_at == fiscal.issued_at
    assert outcome.receipt.total_amount == 747.84
    assert outcome.receipt.external_id == "fiscal:3001101692:10430113"
    assert outcome.receipt.receipt_url == fiscal.raw_url


def test_collapsed_vision_receipt_never_auto_saves(monkeypatch):
    from app.scanner.fiscal_qr import QR_DECODED, QrScan

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(
        QR_DECODED, ["https://cabinet.tax.gov.ua/cashregs/check?id=1&fn=2&sm=747.84"],
    ))

    async def collapsed_ai():
        return {"complete": False, "shop_name": "AZK 17018, BEREZHANY",
                "total_amount": 747.84,
                "items": [{"name": "Американо", "quantity": 1,
                           "unit_price": 747.84, "total_price": 747.84}]}

    outcome = asyncio.run(scan_receipt_bytes(
        _png(400, 800), ai_ocr=collapsed_ai, enable_tax_cabinet=False,
    ))
    assert outcome.confidence == "low"
    assert not outcome.ok
    assert "collapsed/incomplete" in " ".join(outcome.notes)


def test_fiscal_total_is_applied_before_generic_confidence_gate(monkeypatch):
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.scanner.fiscal_qr import QR_DECODED, QrScan
    import datetime as dt

    monkeypatch.setattr("app.scanner.pipeline.scan_qr", lambda _image: QrScan(
        QR_DECODED, ["https://cabinet.tax.gov.ua/cashregs/check?id=1&fn=2&sm=747.84"],
    ))
    monkeypatch.setattr("app.scanner.pipeline.run_ocr", lambda _image: type("O", (), {"text": "Американо 86.00"})())
    monkeypatch.setattr("app.scanner.pipeline.identify_template", lambda _text: None)
    monkeypatch.setattr("app.scanner.pipeline.generic.parse_generic", lambda _text: NormalizedReceipt(
        external_id="", purchased_at=dt.datetime.now(dt.timezone.utc), total_amount=86.0,
        items=[NormalizedItem(name="Американо", quantity=1, unit_price=86, total_price=86)],
    ))
    outcome = asyncio.run(scan_receipt_bytes(_png(400, 800), enable_tax_cabinet=False))
    assert outcome.method == "generic"
    assert outcome.confidence == "low"
    assert outcome.receipt.total_amount == 747.84
    assert outcome.reconciliation.items_sum == 86.0


def test_fiscal_override_links_followup_screenshot_without_visible_qr():
    from app.scanner.fiscal_qr import parse_fiscal_url

    fiscal = parse_fiscal_url(
        "https://cabinet.tax.gov.ua/cashregs/check?date=20260830&time=111656"
        "&id=233747&sm=7061.43&fn=3000893964"
    )

    async def full_screenshot_ai():
        return {"complete": True, "shop_name": "Епіцентр", "total_amount": 7061.43,
                "items": [{"name": "Товар", "quantity": 1,
                           "unit_price": 7061.43, "total_price": 7061.43}]}

    outcome = asyncio.run(scan_receipt_bytes(
        _png(800, 1200), ai_ocr=full_screenshot_ai,
        enable_tax_cabinet=False, fiscal_override=fiscal,
    ))
    assert outcome.ok
    assert outcome.receipt.external_id == "fiscal:3000893964:233747"
    assert outcome.receipt.purchased_at == fiscal.issued_at


def test_pipeline_none_when_nothing_works():
    async def no_ai():
        return None

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=no_ai))
    assert outcome.method == "none"
    assert outcome.receipt is None


# --------------------------------------------------------------------------- #
# A vision reading has nothing behind it — corroborate it or file a draft
# --------------------------------------------------------------------------- #
def test_a_single_line_vision_receipt_is_not_auto_saved():
    """Seen in the wild: for a photo it could not read, the model invented a
    whole different receipt (another shop, another year) with one line equal to
    the total. Such a "balance" is arithmetic against itself, not evidence."""
    async def hallucinating_ai():
        return {"shop_name": "АЗС №13 м. Полтава", "purchased_at": "2023-09-04T14:25:00",
                "total_amount": 970.0,
                "items": [{"name": "Бензин А-95", "quantity": 20.0,
                           "unit_price": 48.5, "total_price": 970.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=hallucinating_ai))
    assert outcome.method == "ai"
    assert outcome.reconciliation.ok, "it balances — that is exactly the trap"
    assert outcome.confidence == "low", "so it must land as a draft, not in the ledger"
    assert any("single line" in n for n in outcome.notes)


def test_a_vision_total_the_photo_never_showed_is_doubted(monkeypatch):
    """The photo's own OCR text is the cheapest fact-check there is."""
    monkeypatch.setattr("app.scanner.pipeline.run_ocr",
                        lambda _image: type("R", (), {"text": "ЕПІЦЕНТР\nСУМА 9253.67 ГРН"})())

    async def wrong_ai():
        return {"shop_name": "АЗС №13", "total_amount": 970.0,
                "items": [{"name": "Бензин", "quantity": 10, "unit_price": 48.5,
                           "total_price": 485.0},
                          {"name": "Кава", "quantity": 1, "unit_price": 485.0,
                           "total_price": 485.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=wrong_ai))
    assert outcome.confidence == "low"
    assert any("немає в тексті" in n for n in outcome.notes)


def test_a_vision_reading_the_photo_backs_up_is_filed(monkeypatch):
    """The guard must not fire on a reading the photo itself confirms."""
    monkeypatch.setattr("app.scanner.pipeline.run_ocr",
                        lambda _image: type("R", (), {"text": "МАГАЗИН\nСУМА 100.00"})())

    async def good_ai():
        return {"shop_name": "МАГАЗИН", "total_amount": 100.0,
                "items": [{"name": "Хліб", "quantity": 1, "unit_price": 60.0,
                           "total_price": 60.0},
                          {"name": "Молоко", "quantity": 1, "unit_price": 40.0,
                           "total_price": 40.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=good_ai))
    assert outcome.confidence == "high"


def test_a_fiscal_qr_still_lets_a_single_line_receipt_through(monkeypatch):
    """Fuel receipts are one line — with the ДПС document behind them there is
    nothing to doubt, so they keep filing themselves."""
    from app.scanner.fiscal_qr import QR_DECODED, FiscalQr, QrScan

    fiscal = FiscalQr(
        raw_url=("https://cabinet.tax.gov.ua/cashregs/check?date=20260912&time=1210"
                 "&id=10430113&sm=970.00&fn=3001101692"),
        fiscal_number="3001101692", document_id="10430113", total_amount=970.0,
    )
    monkeypatch.setattr("app.scanner.pipeline.scan_qr",
                        lambda _image: QrScan(QR_DECODED, [fiscal.raw_url]))

    async def fuel_ai():
        return {"shop_name": "АЗС", "total_amount": 970.0,
                "items": [{"name": "Бензин А-95", "quantity": 20.0,
                           "unit_price": 48.5, "total_price": 970.0}]}

    outcome = asyncio.run(scan_receipt_bytes(_png(400, 1400), ai_ocr=fuel_ai,
                                             enable_tax_cabinet=False))
    assert outcome.confidence == "high"
