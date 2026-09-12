"""Shopping-list tables (ROADMAP.md Крок 2).

Kept in its own module (not app/models/models.py) so it could be built while
Крок 1 was still in flight on that file. Must be imported somewhere before
`Base.metadata.create_all` / Alembic autogenerate runs — see app/main.py.
"""
from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import DateTime, Enum, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class ShoppingListStatus(str, enum.Enum):
    pending = "pending"      # still to buy
    bought = "bought"        # matched to a receipt line — confirmed, or an auto-match candidate (see auto_matched)
    cancelled = "cancelled"  # taken off the active list without buying


class ShoppingListItem(Base):
    """One line on the household shopping list.

    `raw_text` is what the user typed; `normalized_name` is its comparison key
    (app.services.text.normalize_name). When a collected/entered receipt carries
    a matching line the row flips to `bought` with `auto_matched=True` — a
    *candidate* the user then confirms or rejects
    (see app.services.shopping_list, ROADMAP.md Крок 2 "Логіка автовідмітки").
    """

    __tablename__ = "shopping_list_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    raw_text: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String, index=True, default="")
    # FK-less on purpose: the category tree (Крок 1) may not exist yet on a given
    # deployment, and a stale id simply resolves to "uncategorised" in the UI.
    category_id: Mapped[str | None] = mapped_column(String, nullable=True)
    barcode: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float] = mapped_column(Numeric(12, 3), default=1)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[ShoppingListStatus] = mapped_column(
        Enum(ShoppingListStatus), default=ShoppingListStatus.pending, index=True
    )
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    # True while `bought` is only the deterministic matcher's guess and the user
    # has not confirmed it yet.
    auto_matched: Mapped[bool] = mapped_column(default=False)
    # No FK — a ReceiptItem can be deleted independently; a dangling id just
    # reads back as None.
    bought_receipt_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # The receipt line a previous auto-match was rejected against, so the next
    # collection pass doesn't re-propose the exact same pairing.
    rejected_receipt_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    bought_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProductAlias(Base):
    """Cache of "free phrase → store-specific catalogue search query", one row
    per (phrase, store) for all time.

    Populated by `app.services.ai.normalize_query` (ROADMAP.md Крок 2, "Роль
    AI"). The table exists now so the store-suggestion / live-search code has a
    place to read once that lands; nothing writes to it while AI is off.
    """

    __tablename__ = "product_aliases"

    normalized_phrase: Mapped[str] = mapped_column(String, primary_key=True)
    store_code: Mapped[str] = mapped_column(String, primary_key=True)
    query: Mapped[str] = mapped_column(String)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
