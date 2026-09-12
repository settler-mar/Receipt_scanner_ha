import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.api.receipts import list_receipts
from app.models import models
from app.services import duplicates


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    metro = models.Store(code="metro", name="METRO", auth_type=models.AuthType.device_uuid)
    scanner = models.Store(code="scanner", name="Scanner", auth_type=models.AuthType.manual)
    manual = models.Store(code="manual", name="Manual", auth_type=models.AuthType.manual)
    s.add_all([metro, scanner, manual])
    s.flush()
    s.add_all([
        models.Account(store=metro, label="METRO"),
        models.Account(store=scanner, label="Scanner"),
        models.Account(store=manual, label="Manual"),
    ])
    s.commit()
    yield s
    s.close()


def account(db, code):
    return db.query(models.Account).join(models.Store).filter(models.Store.code == code).one()


def receipt(
    db,
    code,
    external,
    when,
    total,
    *,
    raw=None,
    shop="METRO",
    address="проспект A",
    payment="card ****1234",
    items=0,
    item_prefix="Товар",
    tags=(),
):
    r = models.Receipt(
        account=account(db, code),
        store_code=code,
        external_id=external,
        purchased_at=when,
        total_amount=total,
        currency="UAH",
        shop_name=shop,
        shop_address=address,
        payment_type=payment,
        raw=raw or {},
    )
    db.add(r)
    db.flush()
    for i in range(items):
        db.add(models.ReceiptItem(receipt=r, name=f"{item_prefix} {i:02d}", quantity=1, unit="шт", total_price=10 + i))
    for tag in tags:
        db.add(models.ReceiptTag(receipt=r, tag=tag))
    db.commit()
    return r


def test_metro_api_and_fiscal_copy_are_candidate(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 7061.43, raw={"seller_edrpou": "32049199"}, items=15)
    receipt(
        db,
        "scanner",
        "fiscal:4000123456:95123",
        t + dt.timedelta(minutes=3),
        7061.43,
        raw={"method": "fiscal_check", "fn": "4000123456", "id": "95123", "seller_edrpou": "32049199"},
        items=0,
    )

    run = duplicates.run_duplicate_search(db)
    groups = duplicates.list_groups(db)

    assert run.candidates_new == 1
    assert len(groups) == 1
    assert groups[0]["score"] >= 85
    assert "exact amount" in groups[0]["reasons"]
    assert "time within 10m" in groups[0]["reasons"]
    assert "same seller tax id" in groups[0]["reasons"]


def test_repeat_run_is_idempotent(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 7061.43, raw={"rrn": "00112233"})
    receipt(db, "scanner", "s1", t + dt.timedelta(minutes=1), 7061.43, raw={"rrn": "00112233"})

    first = duplicates.run_duplicate_search(db)
    second = duplicates.run_duplicate_search(db)

    assert first.candidates_new == 1
    assert second.candidates_new == 0
    assert len(db.query(models.DuplicateGroup).all()) == 1
    assert len(db.query(models.DuplicateCandidate).all()) == 1


def test_same_store_receipts_are_not_cross_channel_candidates(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 7061.43, raw={"rrn": "00112233"})
    receipt(db, "metro", "m2", t + dt.timedelta(minutes=1), 7061.43, raw={"rrn": "00112233"})

    run = duplicates.run_duplicate_search(db)

    assert run.candidates_new == 0
    assert db.query(models.DuplicateGroup).count() == 0


def test_near_amount_is_not_candidate(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 7061.43, raw={"seller_edrpou": "32049199"})
    receipt(db, "scanner", "s1", t + dt.timedelta(minutes=1), 7061.44, raw={"seller_edrpou": "32049199"})

    run = duplicates.run_duplicate_search(db)

    assert run.candidates_new == 0
    assert db.query(models.DuplicateGroup).count() == 0


def test_far_time_is_not_candidate(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 7061.43, raw={"seller_edrpou": "32049199"})
    receipt(db, "scanner", "s1", t + dt.timedelta(minutes=11), 7061.43, raw={"seller_edrpou": "32049199"})

    run = duplicates.run_duplicate_search(db)

    assert run.candidates_new == 0
    assert db.query(models.DuplicateGroup).count() == 0


def test_ambiguous_same_amount_needs_review(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 100.00, shop="Магазин A", address="Адреса A", payment="cash")
    receipt(db, "scanner", "s1", t + dt.timedelta(minutes=4), 100.00, shop="Магазин B", address="Адреса B", payment="cash")

    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()

    assert group.status == models.DuplicateStatus.needs_review


def test_merge_moves_rich_items_and_preserves_tags_then_rollback(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    rich = receipt(db, "metro", "m1", t, 7061.43, raw={"rrn": "99"}, items=15, tags=("metro_trip",))
    sparse = receipt(db, "scanner", "s1", t + dt.timedelta(minutes=2), 7061.43, raw={"rrn": "99"}, items=1, tags=("tax_copy",))
    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()

    merged = duplicates.merge_group(db, group.id)
    db.refresh(rich)
    db.refresh(sparse)

    assert merged.status == models.DuplicateStatus.auto_merged
    assert merged.canonical_receipt_id == rich.id
    assert len(rich.items) == 15
    assert {t.tag for t in rich.tags} == {"metro_trip", "tax_copy"}
    assert sparse.raw["merged_into_receipt_id"] == rich.id
    assert len(rich.raw["source_receipts"]) == 2
    assert [r["id"] for r in duplicates.group_payload(db, group)["receipts"]] == [rich.id]
    visible = list_receipts(
        db=db,
        store_code=None,
        account_id=None,
        channel=None,
        date_from=None,
        date_to=None,
        search=None,
        tag=None,
        limit=50,
        offset=0,
    )
    assert [r.id for r in visible.items] == [rich.id]

    duplicates.rollback_group(db, group.id)
    db.refresh(rich)
    db.refresh(sparse)

    assert {t.tag for t in rich.tags} == {"metro_trip"}
    assert "merged_into_receipt_id" not in sparse.raw
    assert db.get(models.DuplicateGroup, group.id).status == models.DuplicateStatus.rolled_back


def test_different_shops_same_amount_do_not_auto_merge(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 250.00, shop="METRO", address="A", payment="cash", items=3, item_prefix="M")
    receipt(db, "manual", "x1", t + dt.timedelta(minutes=5), 250.00, shop="FOZZY", address="B", payment="cash", items=3, item_prefix="F")

    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()

    assert group.status == models.DuplicateStatus.needs_review
    assert group.status != models.DuplicateStatus.auto_merged


def test_merge_preserves_discount_and_enriches_item_metadata(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    metro = receipt(db, "metro", "m1", t, 90.00, raw={"rrn": "99"}, items=0)
    scanner = receipt(db, "scanner", "s1", t + dt.timedelta(minutes=2), 90.00, raw={"rrn": "99"}, items=0)
    db.add(models.ReceiptItem(
        receipt=metro,
        name="ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ",
        barcode="4820000000012",
        quantity=1,
        unit="шт",
        unit_price=90,
        total_price=90,
        discount=0,
        product_url="https://example.test/product",
    ))
    db.add(models.ReceiptItem(
        receipt=scanner,
        name="ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ .",
        quantity=1,
        unit="шт",
        unit_price=100,
        total_price=100,
        discount=10,
    ))
    scanner.total_discount = 10
    db.commit()

    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()
    plan = duplicates.group_payload(db, group)["merge_plan"]

    assert plan["items"][0]["result"]["barcode"] == "4820000000012"
    assert plan["items"][0]["result"]["discount"] == 10
    assert plan["items"][0]["result"]["name"] == "ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ"

    duplicates.merge_group(db, group.id)
    db.refresh(metro)

    assert metro.total_discount == 10
    assert len(metro.items) == 1
    assert metro.items[0].barcode == "4820000000012"
    assert metro.items[0].discount == 10
    assert metro.items[0].total_price == 100
    assert metro.items[0].name == "ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ"
    assert metro.items[0].product_url == "https://example.test/product"


def test_merge_plan_shows_receipt_field_differences(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    receipt(db, "metro", "m1", t, 90.00, raw={"rrn": "99"}, address="", payment="", items=1)
    receipt(
        db,
        "scanner",
        "s1",
        t + dt.timedelta(minutes=2),
        90.00,
        raw={"rrn": "99"},
        address="Торговельний центр METRO, Київ",
        payment="card",
        items=1,
    )
    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()

    fields = {
        field["field"]: field
        for field in duplicates.group_payload(db, group)["merge_plan"]["fields"]
        if field["changed"]
    }

    assert fields["shop_address"]["result"] == "Торговельний центр METRO, Київ"
    assert fields["shop_address"]["from_receipt_id"]
    assert fields["payment_type"]["result"] == "card"


def test_merge_honors_manual_choices(db):
    t = dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc)
    metro = receipt(db, "metro", "m1", t, 90.00, raw={"rrn": "99"}, shop="METRO", address="A", items=0)
    scanner = receipt(
        db,
        "scanner",
        "s1",
        t + dt.timedelta(minutes=2),
        90.00,
        raw={"rrn": "99"},
        shop="Fiscal METRO",
        address="Long fiscal address",
        items=0,
    )
    db.add(models.ReceiptItem(
        receipt=metro,
        name="ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ",
        barcode="111",
        quantity=1,
        unit_price=90,
        total_price=90,
        discount=0,
    ))
    db.add(models.ReceiptItem(
        receipt=scanner,
        name="ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ .",
        barcode="222",
        quantity=1,
        unit_price=100,
        total_price=100,
        discount=10,
    ))
    db.commit()
    duplicates.run_duplicate_search(db)
    group = db.query(models.DuplicateGroup).one()

    duplicates.merge_group(db, group.id, choices={
        "receipt_fields": {"shop_address": metro.id},
        "item_name": {"0": scanner.id},
        "item_barcode": {"0": scanner.id},
        "item_price": {"0": metro.id},
        "item_discount": {"0": scanner.id},
    })
    db.refresh(metro)

    assert metro.shop_address == "A"
    assert metro.items[0].name == "ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ"
    assert metro.items[0].barcode == "222"
    assert metro.items[0].total_price == 90
    assert metro.items[0].discount == 10


def test_compare_key_ignores_punctuation_case_spaces_and_latin_confusables():
    assert duplicates._compare_key(" MC  НАГЕТСИ-КУРЯЧІ 1КГ ") == duplicates._compare_key("mc нагетси курячI 1кг.")
    assert duplicates._compare_key("ARO СІК АПЕЛЬСИНОВИЙ 1Л ПЕТ") == duplicates._compare_key("ARO СIК, апельсиновий-1л пет .")
