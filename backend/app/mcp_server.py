"""Own MCP server (ROADMAP.md Крок 4).

The project has been an MCP *client* only (adapters/silpo.py talks to Сільпо's
server). This exposes our own data to an external AI (Claude Desktop, etc.) over
the Model Context Protocol.

Transport: Streamable HTTP, mounted as a sub-app of the FastAPI app at `/mcp`
(see app/main.py). No transport of its own — it rides the same uvicorn process
and the same LAN-only network boundary as the REST API (`cors_origin_regex`).
There is no auth: this is a single-household LAN tool, same threat model as the
REST API it sits next to.

Tools mirror ROADMAP.md Крок 4:

  read:
    * search_products(query)            — prices per chain from receipt history
    * spending_summary(from, to, ...)   — totals, incl. by the category tree
    * list_receipts(from, to, store?)   — receipt list (slim rows)
    * suggest_store()                   — best chain for the current shopping list

  write:
    * add_to_shopping_list(text, qty?, unit?, store?, category?, tag?) — shopping-list items
    * list_shopping_items / update_shopping_item / bulk_update_shopping_items
    * categorize_shopping_list / shopping_archive / create_shopping_trip
    * add_receipt(total, items?, ...)          — a purchase/receipt into the DB

Which tools are actually reachable is decided at request time by
`app/services/mcp_config.py` (Settings page): the whole server can be switched
off, and every tool has its own access level (off / read / read+write).
`tools/list` and `tools/call` both honour it — see `_ConfigGatedMCP` below.

`suggest_store` / `add_to_shopping_list` also keep a defensive
`_shopping_enabled()` check: if the `shopping_list_items` table is missing (old
DB, migration not run), the tool returns a clear "not enabled yet" message.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool
from sqlalchemy import func, inspect as sa_inspect, select

from app.db import SessionLocal, engine
from app.models.models import Account, AccountStatus, FuelPriceSnapshot, Receipt, ReceiptItem, Store
from app.services.mcp_config import load_config


class _ConfigGatedMCP(FastMCP):
    """FastMCP that filters tools through `mcp_config` (Settings page) on every
    request, so the per-tool access levels apply live without a restart."""

    def _config(self):
        with SessionLocal() as db:
            return load_config(db)

    async def list_tools(self, *args, **kwargs) -> list[Tool]:
        cfg = self._config()
        return [t for t in await super().list_tools(*args, **kwargs) if cfg.tool_available(t.name)]

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        cfg = self._config()
        if not cfg.tool_available(name):
            if not cfg.enabled:
                detail = "MCP-сервер вимкнено в налаштуваннях"
            else:
                detail = f"інструмент {name!r} має рівень доступу '{cfg.access_for(name)}' у налаштуваннях"
            raise ToolError(detail)
        return await super().call_tool(name, arguments)


mcp = _ConfigGatedMCP(
    "receipt-scanner",
    instructions=(
        "Read access to a household's grocery/fuel receipt history and spending, "
        "plus write access to its shopping list and to record new receipts. All "
        "amounts are UAH unless a row says otherwise. Dates are ISO-8601; naive "
        "values are treated as UTC. Individual tools may be disabled server-side."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # DNS-rebinding Host/Origin checks off: same reasoning as the REST API's
    # cors_origin_regex — this is a LAN box whose own IP isn't stable, reached
    # from arbitrary devices on the same network. The network is the boundary.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _parse_dt(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError as exc:  # surfaced to the caller as a tool error
        raise ValueError(f"не розумію дату {value!r} — потрібен формат ISO-8601") from exc


def _shopping_enabled() -> bool:
    return sa_inspect(engine).has_table("shopping_list_items")


# Store lookup, station prices and the fuel receipt itself live in
# services/fuel_entry.py — the Telegram bot's free-phrase capture records the very
# same purchase and must do it by exactly the same rules.
from app.services.fuel_entry import (  # noqa: E402
    latest_fuel_price as _latest_fuel_price,
    record_fuel_purchase as _record_fuel_purchase,
    resolve_store as _resolve_store,
    store_account as _store_account,
)


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_products(query: str) -> list[dict[str, Any]]:
    """Search past receipt items by name (substring, case-insensitive) and return
    price stats per (product name, store): last/min/max/avg price, unit, how many
    times bought, when last seen. This is receipt history — not a live catalog."""
    from app.api.products import search_products as _search

    with SessionLocal() as db:
        return [row.model_dump(mode="json") for row in _search(q=query, db=db)]


@mcp.tool()
def spending_summary(
    date_from: str | None = None,
    date_to: str | None = None,
    group_by: str = "total",
) -> dict[str, Any]:
    """Spending totals over an optional date window.

    group_by:
      * "total"    — receipts count, total spent, discount, bonuses
      * "store"    — one row per chain
      * "category" — one row per category-tree branch (direct + rollup totals)
      * "month"    — time series (auto day/month/year bucket by span)
    """
    from app.api.dashboard import by_category, by_store, get_stats, timeseries

    lo, hi = _parse_dt(date_from), _parse_dt(date_to)
    with SessionLocal() as db:
        if group_by == "total":
            return get_stats(db=db, date_from=lo, date_to=hi).model_dump(mode="json")
        if group_by == "store":
            rows = by_store(db=db, date_from=lo, date_to=hi)
        elif group_by == "category":
            rows = by_category(db=db, date_from=lo, date_to=hi)
        elif group_by == "month":
            rows = timeseries(db=db, date_from=lo, date_to=hi, bucket="auto")
        else:
            raise ValueError("group_by має бути total | store | category | month")
    return {"group_by": group_by, "rows": [r.model_dump(mode="json") for r in rows]}


@mcp.tool()
def list_receipts(
    date_from: str | None = None,
    date_to: str | None = None,
    store_code: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List receipts newest-first as slim rows (id, date, store, shop, total,
    item_count, discount). Use get via the REST API for full item detail."""
    lo, hi = _parse_dt(date_from), _parse_dt(date_to)
    limit = max(1, min(limit, 200))

    clauses = []
    if lo:
        clauses.append(Receipt.purchased_at >= lo)
    if hi:
        clauses.append(Receipt.purchased_at <= hi)
    if store_code:
        clauses.append(Receipt.store_code == store_code)

    with SessionLocal() as db:
        store_names = {s.code: s.name for s in db.execute(select(Store)).scalars().all()}
        total = db.execute(select(func.count()).select_from(Receipt).where(*clauses)).scalar_one()
        item_counts = dict(
            db.execute(
                select(ReceiptItem.receipt_id, func.count())
                .group_by(ReceiptItem.receipt_id)
            ).all()
        )
        rows = db.execute(
            select(Receipt).where(*clauses).order_by(Receipt.purchased_at.desc()).limit(limit)
        ).scalars().all()

        items = [
            {
                "id": r.id,
                "purchased_at": r.purchased_at.isoformat() if r.purchased_at else None,
                "store_code": r.store_code,
                "store_name": store_names.get(r.store_code, r.store_code),
                "shop_name": r.shop_name,
                "channel": r.channel,
                "total_amount": float(r.total_amount) if r.total_amount is not None else None,
                "total_discount": float(r.total_discount) if r.total_discount is not None else None,
                "currency": r.currency,
                "item_count": item_counts.get(r.id, 0),
            }
            for r in rows
        ]
    return {"total": total, "returned": len(items), "items": items}


@mcp.tool()
def suggest_store() -> dict[str, Any]:
    """Rank store chains for the current pending shopping list by how many items
    each has covered in receipt history and the approximate basket cost. History
    only — no live price calls."""
    if not _shopping_enabled():
        return {"enabled": False, "message": "Список покупок ще не увімкнено (ROADMAP Крок 2)."}
    from app.services import shopping_list as svc

    with SessionLocal() as db:
        return {"enabled": True, "stores": svc.suggest_stores(db)}


# --------------------------------------------------------------------------- #
# Write tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def add_receipt(
    total_amount: float | None = None,
    purchased_at: str | None = None,
    shop_name: str | None = None,
    items: list[dict[str, Any]] | None = None,
    currency: str = "UAH",
    payment_type: str | None = None,
    total_discount: float | None = None,
    shop_address: str | None = None,
) -> dict[str, Any]:
    """Record a purchase (a receipt) into the database — a paper receipt or one
    from a store the app doesn't automate.

    Saved under the built-in "scanner" account (store_code="scanner"), the same
    path the receipt scanner uses: deduplicated, then run through category
    tagging and shopping-list auto-tick.

    * `purchased_at` — ISO-8601; defaults to now (UTC).
    * `items` — optional list of `{name, total_price, quantity?, unit?,
      unit_price?, barcode?, product_code?, discount?, category?}`. A line may give
      `unit_price` + `quantity` instead of `total_price` ("0.7 кг по 50") — the
      product is computed here. A receipt with just a total and no items is allowed.
    * `total_amount` — optional when there are items: it then defaults to their
      sum. Required for a receipt with no lines.
    * `total_discount` — receipt-level; defaults to the sum of item discounts.
    """
    from app.adapters.base import NormalizedItem, NormalizedReceipt
    from app.api.inbox import _persist_receipt, _scanner_account

    when = _parse_dt(purchased_at) or dt.datetime.now(dt.timezone.utc)

    norm_items: list[NormalizedItem] = []
    for raw in items or []:
        quantity = float(raw.get("quantity", 1) or 1)
        line_total = raw.get("total_price")
        if line_total is None and raw.get("unit_price") is not None:
            line_total = round(quantity * float(raw["unit_price"]), 2)
        if not raw.get("name") or line_total is None:
            raise ValueError("кожна позиція потребує 'name' і 'total_price' (або 'unit_price')")
        norm_items.append(NormalizedItem(
            name=str(raw["name"]).strip(),
            total_price=float(line_total),
            quantity=quantity,
            unit=raw.get("unit"),
            unit_price=float(raw["unit_price"]) if raw.get("unit_price") is not None else None,
            barcode=raw.get("barcode"),
            product_code=raw.get("product_code") or raw.get("barcode"),
            category=raw.get("category"),
            discount=float(raw.get("discount", 0) or 0),
        ))

    if total_amount is None:
        if not norm_items:
            raise ValueError("потрібна 'total_amount' або хоча б одна позиція")
        total_amount = round(sum(float(i.total_price) for i in norm_items), 2)

    nr = NormalizedReceipt(
        external_id=f"mcp:{dt.datetime.now(dt.timezone.utc).timestamp():.0f}:{abs(hash((when, total_amount, shop_name))) % 10**8}",
        purchased_at=when,
        total_amount=float(total_amount),
        currency=currency or "UAH",
        shop_name=shop_name,
        shop_address=shop_address,
        payment_type=payment_type,
        channel="offline",
        items=norm_items,
        total_discount=total_discount,
        raw={"source": "mcp"},
    )

    with SessionLocal() as db:
        try:
            receipt = _persist_receipt(db, _scanner_account(db), nr)
        except Exception as exc:  # HTTPException (scanner store not seeded) etc.
            raise ValueError(f"не вдалося зберегти чек: {getattr(exc, 'detail', exc)}") from exc
        item_sum = sum(float(i.total_price) - float(i.discount or 0) for i in receipt.items)
        return {
            "id": receipt.id,
            "store_code": receipt.store_code,
            "external_id": receipt.external_id,
            "purchased_at": receipt.purchased_at.isoformat() if receipt.purchased_at else None,
            "total_amount": float(receipt.total_amount) if receipt.total_amount is not None else None,
            "item_count": len(receipt.items),
            "shop_name": receipt.shop_name,
            # a nudge for the caller when the lines don't add up to the total
            "items_reconcile": abs(item_sum - float(total_amount)) < 0.02 if receipt.items else None,
        }


@mcp.tool()
def add_fuel_purchase(
    store: str,
    fuel: str = "газ",
    amount: float | None = None,
    liters: float | None = None,
    price_per_liter: float | None = None,
    purchased_at: str | None = None,
) -> dict[str, Any]:
    """Record a fuel purchase and attach it to a real store account.

    Give the `store` (name or code — "барс", "bars", "БРСМ"…), the `fuel`
    ("газ" / "дизель" / "бензин" / an exact product name) and **either**
    `amount` (UAH paid) **or** `liters` — the other is computed from the
    station's current price. `price_per_liter` overrides that lookup.

    The price comes from the latest saved fuel-price snapshot for that station
    (Dashboard → fuel prices keeps it fresh). The receipt is one line
    (fuel name, quantity in litres, unit price) under that store's account —
    same dedup / category / shopping-list path as every other receipt.

    Examples:
      add_fuel_purchase(store="барс", fuel="газ", amount=1639.53)
      add_fuel_purchase(store="барс", fuel="газ", liters=40)
    """
    when = _parse_dt(purchased_at)

    with SessionLocal() as db:
        return _record_fuel_purchase(
            db, store=store, fuel=fuel, amount=amount, liters=liters,
            price_per_liter=price_per_liter, purchased_at=when,
        )


@mcp.tool()
def add_to_shopping_list(
    text: str | None = None,
    qty: float = 1,
    unit: str | None = None,
    note: str | None = None,
    target_store_code: str | None = None,
    category_id: str | None = None,
    tag: str | None = None,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add items to the shopping list. `text` is a free phrase ("молоко 2.5%");
    it is normalized on save. A freshly added item is immediately checked against
    recent receipts and may come back already `bought`.

    One spoken list usually holds several things ("яйця, морква, молоко для
    Мілани"), so `items` takes them in one call: a list of
    `{text, qty?, unit?, note?, target_store_code?, category_id?, tag?}`.
    `text` and `items` may be combined. A store/category/tag supplied here is
    explicit metadata, not a recommendation.

    Delegates to the same `POST /api/shopping-list` handler the UI uses, so
    behaviour stays in sync with Крок 2."""
    requests: list[dict[str, Any]] = []
    if text and text.strip():
        requests.append({"text": text.strip(), "qty": qty, "unit": unit, "note": note,
                         "target_store_code": target_store_code, "category_id": category_id, "tag": tag})
    for raw in items or []:
        entry = str(raw.get("text") or "").strip()
        if not entry:
            raise ValueError("кожна позиція потребує 'text'")
        requests.append({
            "text": entry, "qty": float(raw.get("qty", 1) or 1),
            "unit": raw.get("unit"), "note": raw.get("note"),
            "target_store_code": raw.get("target_store_code"),
            "category_id": raw.get("category_id"), "tag": raw.get("tag"),
        })
    if not requests:
        raise ValueError("порожній текст")
    if not _shopping_enabled():
        return {"enabled": False, "message": "Список покупок ще не увімкнено (ROADMAP Крок 2)."}

    from app.api.shopping_list import create_item
    from app.schemas.shopping import ShoppingItemCreate

    with SessionLocal() as db:
        rows = [
            create_item(ShoppingItemCreate(raw_text=r["text"], qty=r["qty"],
                                           unit=r["unit"], note=r["note"],
                                           target_store_code=r["target_store_code"],
                                           category_id=r["category_id"], tag=r["tag"]), db=db)
            for r in requests
        ]
        out = [_shopping_row(db, row) for row in rows]
        # A single-item call keeps its old flat shape; callers that only ever add
        # one thing should not have to learn a list.
        return {"enabled": True, "items": out, **out[0]} if len(out) == 1 else {
            "enabled": True, "items": out,
        }


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _shopping_row(db, row) -> dict[str, Any]:
    from app.api.shopping_list import _item_out
    return _jsonable(_item_out(db, row))


@mcp.tool()
def list_shopping_items(status: str = "pending") -> list[dict[str, Any]]:
    """List shopping rows with category, explicit store, tag and purchase data.

    `status` is `pending`, `bought`, `cancelled` or `all`.
    """
    if not _shopping_enabled():
        return []
    from app.models.shopping import ShoppingListItem, ShoppingListStatus
    with SessionLocal() as db:
        stmt = select(ShoppingListItem)
        if status != "all":
            try:
                stmt = stmt.where(ShoppingListItem.status == ShoppingListStatus(status))
            except ValueError as exc:
                raise ValueError("status має бути pending | bought | cancelled | all") from exc
        rows = db.execute(stmt.order_by(ShoppingListItem.added_at.desc())).scalars().all()
        return [_shopping_row(db, row) for row in rows]


@mcp.tool()
def update_shopping_item(
    item_id: str,
    raw_text: str | None = None,
    qty: float | None = None,
    note: str | None = None,
    category_id: str | None = None,
    target_store_code: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    clear_category: bool = False,
    clear_store: bool = False,
    clear_tag: bool = False,
) -> dict[str, Any]:
    """Edit one shopping row, including its category, explicit store and tag."""
    from app.api.shopping_list import update_item
    from app.schemas.shopping import ShoppingItemUpdate
    data = {}
    if raw_text is not None: data["raw_text"] = raw_text
    if qty is not None: data["qty"] = qty
    if note is not None: data["note"] = note
    if category_id is not None: data["category_id"] = category_id
    if target_store_code is not None: data["target_store_code"] = target_store_code
    if tag is not None: data["tag"] = tag
    if status is not None: data["status"] = status
    if clear_category: data["category_id"] = None
    if clear_store: data["target_store_code"] = None
    if clear_tag: data["tag"] = None
    payload = ShoppingItemUpdate.model_validate(data)
    with SessionLocal() as db:
        try:
            return _jsonable(update_item(item_id, payload, db))
        except Exception as exc:
            raise ValueError(str(exc)) from exc


@mcp.tool()
def bulk_update_shopping_items(
    item_ids: list[str],
    status: str | None = None,
    category_id: str | None = None,
    target_store_code: str | None = None,
    tag: str | None = None,
    clear_category: bool = False,
    clear_store: bool = False,
    clear_tag: bool = False,
) -> dict[str, Any]:
    """Apply the same status/category/store/tag operation to selected rows."""
    if not item_ids:
        raise ValueError("item_ids не може бути порожнім")
    results = []
    from app.api.shopping_list import update_item
    from app.schemas.shopping import ShoppingItemUpdate
    with SessionLocal() as db:
        for item_id in item_ids:
            data = {}
            if status is not None: data["status"] = status
            if category_id is not None: data["category_id"] = category_id
            if target_store_code is not None: data["target_store_code"] = target_store_code
            if tag is not None: data["tag"] = tag
            if clear_category: data["category_id"] = None
            if clear_store: data["target_store_code"] = None
            if clear_tag: data["tag"] = None
            results.append(_jsonable(update_item(item_id, ShoppingItemUpdate.model_validate(data), db)))
    return {"updated": len(results), "items": results}


@mcp.tool()
async def categorize_shopping_list() -> dict[str, Any]:
    """Categorize pending rows: local dictionaries first, then AI for the rest."""
    if not _shopping_enabled():
        return {"checked": 0, "categorized": 0}
    from app.services import shopping_list as svc
    with SessionLocal() as db:
        return await svc.categorize_pending(db)


@mcp.tool()
def shopping_archive() -> list[dict[str, Any]]:
    """Return receipt-backed completed shopping rows grouped by receipt."""
    from app.api.shopping_list import archive
    with SessionLocal() as db:
        return _jsonable(archive(db))


@mcp.tool()
def create_shopping_trip(store_code: str | None = None) -> dict[str, Any]:
    """Create a checklist snapshot for a selected store or all stores."""
    from app.api.shopping_list import _trip_payload
    from app.services import shopping_list as svc
    with SessionLocal() as db:
        return _jsonable(_trip_payload(db, svc.create_trip(db, store_code=store_code)))


@mcp.tool()
async def capture_text(text: str) -> dict[str, Any]:
    """Read one free phrase the way the Telegram bot does and act on it.

    "додай чек перукарня Вікторія на Індустріальній, стрижка - 600" files a
    receipt draft for review; "додай в список покупок яйця, морква" adds the
    entries. Prices are read as said — "- 600" is the line total, "по 50" is the
    price per unit — and every sum is recomputed server-side.

    Use it when handing over the user's sentence verbatim; when the wording is
    already broken down, `add_receipt` / `add_to_shopping_list` are more direct.
    "був на барс і заправився газом на 1000грн" is recorded as a fuel receipt
    right away — station, fuel and price all come from the user's own data, and
    the missing half (litres or sum) is computed from the station's saved price.

    Returns `{"intent": "receipt"}` with the draft and its scan-job id (nothing
    is booked until the draft is accepted in the app), `{"intent": "fuel"}` with
    the saved purchase, `{"intent": "shopping"}` with the added rows, or
    `{"intent": "unknown"}` when the phrase was none of them.
    """
    from app.services import capture

    text = (text or "").strip()
    if not text:
        raise ValueError("порожній текст")

    with SessionLocal() as db:
        draft = await capture.parse_capture(db, text)
        if draft.intent == "receipt":
            job = capture.create_receipt_job(db, draft, method="mcp")
            return {"intent": "receipt", "scan_job_id": job.id,
                    "needs_review": True, "draft": draft.draft_dict()}
        if draft.intent == "fuel":
            return {"intent": "fuel", **capture.record_fuel(db, draft)}
        if draft.intent == "shopping":
            if not _shopping_enabled():
                return {"intent": "shopping", "enabled": False,
                        "message": "Список покупок ще не увімкнено (ROADMAP Крок 2)."}
            rows = capture.add_shopping_items(db, draft, added_method="mcp", added_by="MCP")
            return {"intent": "shopping", "enabled": True,
                    "items": [_shopping_row(db, row) for row in rows]}
        return {"intent": "unknown",
                "message": "Не схоже ні на чек, ні на список покупок."}


# Starlette sub-app mounted by app/main.py; its lifespan runs the MCP session
# manager and must be entered from the parent app's lifespan.
mcp_app = mcp.streamable_http_app()
