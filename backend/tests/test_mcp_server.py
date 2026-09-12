"""Own MCP server tools (ROADMAP.md Крок 4).

The tool functions talk to the DB through `app.mcp_server.SessionLocal` directly
(same pattern as scheduler.py), so the fixture points that at a temp DB and also
seeds the category tree the `spending_summary(group_by="category")` path needs.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import mcp_server
from app.db import Base
from app.models import models
from app.models import shopping as shopping_models  # noqa: F401 — register tables
from app.models.models import (
    Account,
    AccountStatus,
    FuelPriceSnapshot,
    Receipt,
    ReceiptItem,
    Store,
)
from app.models.shopping import ShoppingListItem, ShoppingListStatus


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(mcp_server, "SessionLocal", TestingSession)
    monkeypatch.setattr(mcp_server, "engine", engine)

    s = TestingSession()
    store = Store(code="atb", name="АТБ", auth_type=models.AuthType.manual,
                  network_type="supermarket")
    s.add(store)
    # add_receipt saves under the built-in "scanner" store (seeded in prod)
    s.add(Store(code="scanner", name="Сканер (OCR)", auth_type=models.AuthType.manual))
    s.flush()
    acc = Account(store_id=store.id, label="test")
    s.add(acc)
    s.flush()
    r = Receipt(
        account_id=acc.id, store_code="atb", external_id="x1",
        purchased_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3),
        total_amount=100, currency="UAH",
        shop_name="АТБ №1", channel="offline", total_discount=5,
    )
    s.add(r)
    s.flush()
    s.add(ReceiptItem(receipt_id=r.id, name="Молоко Яготинське 2.5%", quantity=2,
                      unit="шт", unit_price=40, total_price=80, discount=0))
    s.add(ReceiptItem(receipt_id=r.id, name="Хліб", quantity=1, unit="шт",
                      unit_price=25, total_price=25, discount=5))

    # Fuel station with a saved price snapshot — for add_fuel_purchase.
    bars = Store(code="bars", name="АЗК Барс", auth_type=models.AuthType.manual,
                 network_type="azs")
    s.add(bars)
    s.flush()
    bars_acc = Account(store_id=bars.id, label="Барс", status=AccountStatus.active)
    s.add(bars_acc)
    s.flush()
    s.add(FuelPriceSnapshot(
        account_id=bars_acc.id, store_code="bars", product_code="1",
        product_name="Газ скраплений", price=42.19, currency="UAH",
        recorded_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1),
    ))
    s.commit()
    s.close()
    yield TestingSession


def test_search_products(db):
    rows = mcp_server.search_products("Молоко")
    assert len(rows) == 1
    assert rows[0]["store_code"] == "atb"
    assert rows[0]["last_price"] == 40.0
    assert rows[0]["times_bought"] == 1


def test_search_products_too_short(db):
    assert mcp_server.search_products("a") == []


def test_spending_summary_total(db):
    out = mcp_server.spending_summary(group_by="total")
    assert out["total_receipts"] == 1
    assert out["total_spent"] == 100.0


def test_spending_summary_store(db):
    out = mcp_server.spending_summary(group_by="store")
    assert out["group_by"] == "store"
    assert out["rows"][0]["store_code"] == "atb"


def test_spending_summary_bad_group(db):
    with pytest.raises(ValueError):
        mcp_server.spending_summary(group_by="nonsense")


def test_spending_summary_bad_date(db):
    with pytest.raises(ValueError):
        mcp_server.spending_summary(date_from="last tuesday")


def test_list_receipts(db):
    out = mcp_server.list_receipts()
    assert out["total"] == 1
    row = out["items"][0]
    assert row["store_name"] == "АТБ"
    assert row["item_count"] == 2
    assert row["total_amount"] == 100.0


def test_list_receipts_store_filter(db):
    assert mcp_server.list_receipts(store_code="silpo")["total"] == 0


def test_shopping_tools_degrade_when_not_enabled(db, monkeypatch):
    # Old DB / migration not run -> clear "not enabled" reply, not a crash.
    monkeypatch.setattr(mcp_server, "_shopping_enabled", lambda: False)
    assert mcp_server.suggest_store()["enabled"] is False
    assert mcp_server.add_to_shopping_list("молоко")["enabled"] is False


def test_add_to_shopping_list(db):
    res = mcp_server.add_to_shopping_list("Кава мелена", qty=2, unit="пач")
    assert res["enabled"] is True
    assert res["normalized_name"]
    assert res["qty"] == 2
    with db() as s:
        rows = s.query(ShoppingListItem).all()
        assert [r.raw_text for r in rows] == ["Кава мелена"]


def test_add_to_shopping_list_empty(db):
    with pytest.raises(ValueError):
        mcp_server.add_to_shopping_list("   ")


def test_add_to_shopping_list_auto_matched_from_receipt(db):
    # "Хліб" is on the seeded receipt (3 days ago) -> rescan flips it to bought.
    res = mcp_server.add_to_shopping_list("Хліб")
    assert res["status"] == "bought"
    assert res["auto_matched"] is True


def test_suggest_store_enabled_empty_list(db):
    out = mcp_server.suggest_store()
    assert out["enabled"] is True
    assert out["stores"] == []


def test_suggest_store_ranks_by_coverage(db):
    with db() as s:
        s.add(ShoppingListItem(raw_text="Хліб", normalized_name="хліб",
                               qty=1, status=ShoppingListStatus.pending))
        s.commit()
    out = mcp_server.suggest_store()
    assert out["stores"][0]["store_code"] == "atb"
    assert out["stores"][0]["covered_items"] == 1


# --------------------------------------------------------------------------- #
# Config gating (Settings page — mcp_config)
# --------------------------------------------------------------------------- #
from app.services import mcp_config as mcp_cfg  # noqa: E402


def _save_cfg(db, **kw):
    with db() as s:
        mcp_cfg.save_config(s, mcp_cfg.McpConfig(**kw))


def test_config_defaults_per_tool():
    cfg = mcp_cfg.McpConfig()
    assert cfg.enabled
    assert cfg.access_for("search_products") == "read"
    assert cfg.access_for("add_to_shopping_list") == "off"   # write tool closed by default
    assert cfg.tool_available("search_products")
    assert not cfg.tool_available("add_to_shopping_list")
    assert not cfg.tool_available("add_receipt")


def test_config_grant_write_per_tool():
    cfg = mcp_cfg.McpConfig(tools={"add_to_shopping_list": "write"})
    assert cfg.tool_available("add_to_shopping_list")
    assert not cfg.tool_available("add_receipt")  # independent — still off


def test_config_read_level_does_not_grant_write():
    cfg = mcp_cfg.McpConfig(tools={"add_receipt": "read"})
    assert not cfg.tool_available("add_receipt")


def test_config_per_tool_off_and_unknown_dropped():
    cfg = mcp_cfg.McpConfig(tools={"search_products": "off", "bogus": "write"})
    assert not cfg.tool_available("search_products")
    assert "bogus" not in cfg.tools
    assert cfg.access_for("list_receipts") == "read"  # missing key -> default


def test_config_legacy_bool_shape_upgrades():
    # old blob: {mode: "read_write", tools: {name: bool}} -> bools become read/off
    cfg = mcp_cfg.McpConfig.model_validate(
        {"enabled": True, "mode": "read_write",
         "tools": {"search_products": True, "list_receipts": False, "add_receipt": True}}
    )
    assert cfg.access_for("search_products") == "read"
    assert cfg.access_for("list_receipts") == "off"
    assert cfg.access_for("add_receipt") == "read"       # not silently "write"
    assert not cfg.tool_available("add_receipt")


def test_config_disabled_server_hides_everything():
    cfg = mcp_cfg.McpConfig(enabled=False, tools={"search_products": "read"})
    assert not cfg.tool_available("search_products")


def test_gated_list_and_call(db):
    import asyncio

    import mcp.server.fastmcp.exceptions as mcp_exc

    _loop = asyncio.new_event_loop()
    run = _loop.run_until_complete

    def names():
        return [t.name for t in run(mcp_server.mcp.list_tools())]

    # default: write tools absent from list, error on call
    assert "search_products" in names()
    assert "add_to_shopping_list" not in names()
    assert "add_receipt" not in names()
    with pytest.raises(mcp_exc.ToolError):
        run(mcp_server.mcp.call_tool("add_to_shopping_list", {"text": "x"}))

    # grant write to just add_to_shopping_list
    _save_cfg(db, tools={"add_to_shopping_list": "write"})
    assert "add_to_shopping_list" in names()
    assert "add_receipt" not in names()

    # turn a read tool off
    _save_cfg(db, tools={"add_to_shopping_list": "write", "list_receipts": "off"})
    assert "list_receipts" not in names()
    with pytest.raises(mcp_exc.ToolError):
        run(mcp_server.mcp.call_tool("list_receipts", {}))
    _loop.close()


def test_add_receipt(db):
    _save_cfg(db, tools={"add_receipt": "write"})
    res = mcp_server.add_receipt(
        total_amount=125.5,
        shop_name="Магазин біля дому",
        items=[
            {"name": "Молоко", "total_price": 40.0, "quantity": 2, "unit_price": 20.0},
            {"name": "Хліб", "total_price": 85.5},
        ],
    )
    assert res["store_code"] == "scanner"
    assert res["item_count"] == 2
    assert res["total_amount"] == 125.5
    assert res["items_reconcile"] is True
    with db() as s:
        r = s.get(Receipt, res["id"])
        assert r is not None and len(r.items) == 2


def test_add_receipt_bad_item(db):
    with pytest.raises(ValueError):
        mcp_server.add_receipt(total_amount=10, items=[{"name": "no price"}])


# --------------------------------------------------------------------------- #
# add_fuel_purchase
# --------------------------------------------------------------------------- #
def test_fuel_purchase_by_amount(db):
    res = mcp_server.add_fuel_purchase(store="барс", fuel="газ", amount=1639.53)
    assert res["store_code"] == "bars"
    assert res["account_label"] == "Барс"
    assert res["fuel"] == "Газ скраплений"
    assert res["price_per_liter"] == 42.19
    assert res["liters"] == round(1639.53 / 42.19, 3)
    assert res["total_amount"] == 1639.53
    with db() as s:
        r = s.get(Receipt, res["id"])
        assert r.store_code == "bars" and r.account_id is not None
        assert len(r.items) == 1 and r.items[0].unit == "л"


def test_fuel_purchase_by_liters(db):
    res = mcp_server.add_fuel_purchase(store="bars", fuel="газ", liters=40)
    assert res["liters"] == 40.0
    assert res["total_amount"] == round(40 * 42.19, 2)


def test_fuel_purchase_price_override(db):
    res = mcp_server.add_fuel_purchase(
        store="Барс", fuel="газ", liters=10, price_per_liter=40.0
    )
    assert res["price_per_liter"] == 40.0
    assert res["total_amount"] == 400.0
    assert res["price_source"] == "передано явно"


def test_fuel_purchase_needs_amount_or_liters(db):
    with pytest.raises(ValueError):
        mcp_server.add_fuel_purchase(store="барс", fuel="газ")


def test_fuel_purchase_unknown_store(db):
    with pytest.raises(ValueError):
        mcp_server.add_fuel_purchase(store="няшка", fuel="газ", amount=100)


def test_fuel_purchase_unknown_fuel(db):
    with pytest.raises(ValueError, match="Доступні|паливо"):
        mcp_server.add_fuel_purchase(store="барс", fuel="гелій", amount=100)


# --------------------------------------------------------------------------- #
# Free-phrase capture (CAPTURE_PLAN.md)
# --------------------------------------------------------------------------- #
def test_add_receipt_totals_itself_from_unit_priced_lines(db):
    """«помидоры 2кг - 160грн. огурцы 0,7кг по 50грн» handed over as lines: the
    per-unit line and the receipt total are both computed here."""
    _save_cfg(db, tools={"add_receipt": "write"})
    res = mcp_server.add_receipt(
        shop_name="покупка у бабули",
        items=[
            {"name": "помідори", "quantity": 2, "unit": "кг", "total_price": 160},
            {"name": "огірки", "quantity": 0.7, "unit": "кг", "unit_price": 50},
        ],
    )
    assert res["total_amount"] == 195.0
    assert res["items_reconcile"] is True


def test_add_receipt_without_items_still_needs_a_total(db):
    with pytest.raises(ValueError):
        mcp_server.add_receipt()


def test_add_to_shopping_list_takes_a_whole_list(db):
    res = mcp_server.add_to_shopping_list(items=[
        {"text": "яйця", "qty": 3, "unit": "десяток"},
        {"text": "морква"},
        {"text": "молоко", "note": "для Мілани"},
    ])
    assert [i["raw_text"] for i in res["items"]] == ["яйця", "морква", "молоко"]
    with db() as s:
        assert s.query(ShoppingListItem).count() == 3


def test_capture_text_reads_a_phrase_into_the_shopping_list(db, monkeypatch):
    import asyncio

    from app.services import capture

    async def fake_parse(session, text):
        return capture.parse_payload({
            "intent": "shopping", "confidence": 0.9,
            "shopping": [{"text": "яйця", "qty": 3, "unit": "десяток"},
                         {"text": "молоко", "note": "для Мілани"}],
        }, source_text=text)

    monkeypatch.setattr(capture, "parse_capture", fake_parse)
    out = asyncio.run(mcp_server.capture_text("додай в список покупок яйця 3 десятка, молоко"))
    assert out["intent"] == "shopping"
    assert [i["raw_text"] for i in out["items"]] == ["яйця", "молоко"]


def test_capture_text_files_a_receipt_for_review(db, monkeypatch):
    import asyncio

    from app.models.scanner import ScanJob
    from app.services import capture

    async def fake_parse(session, text):
        return capture.parse_payload({
            "intent": "receipt", "confidence": 0.9,
            "receipt": {"shop_name": "Перукарня Вікторія",
                        "items": [{"name": "Стрижка", "total_price": 600}]},
        }, source_text=text)

    monkeypatch.setattr(capture, "parse_capture", fake_parse)
    out = asyncio.run(mcp_server.capture_text("додай чек перукарня Вікторія, стрижка - 600"))
    assert out["intent"] == "receipt" and out["needs_review"] is True
    assert out["draft"]["total_amount"] == 600
    with db() as s:
        # Read, not booked — the draft waits for the user.
        assert s.get(ScanJob, out["scan_job_id"]) is not None
        assert s.query(Receipt).filter(Receipt.store_code == "scanner").count() == 0


def test_capture_text_records_a_refuelling(db, monkeypatch):
    """«був на барс і залив 15л газа» — the sum comes from the station's price."""
    import asyncio

    from app.services import capture

    async def fake_parse(session, text):
        return capture.parse_payload({
            "intent": "fuel", "confidence": 0.9,
            "fuel": {"store": "барс", "fuel": "газ", "liters": 15},
        }, source_text=text)

    monkeypatch.setattr(capture, "parse_capture", fake_parse)
    out = asyncio.run(mcp_server.capture_text("був на барс і залив 15л газа"))
    assert out["intent"] == "fuel"
    assert out["liters"] == 15.0
    assert out["total_amount"] == round(15 * out["price_per_liter"], 2)
