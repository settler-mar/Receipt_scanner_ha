"""Reusable policy and persistence helpers for mutable order delivery data."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Account, Receipt


@dataclass
class DeliveryUpdate:
    status: str | None = None
    tracking_numbers: list[str] = field(default_factory=list)


@runtime_checkable
class OrderTrackingAdapter(Protocol):
    async def refresh_order_tracking(
        self,
        access_token: str,
        extra: dict,
        active_orders: dict[str, str | None],
    ) -> dict[str, DeliveryUpdate]: ...


class OrderDeliveryUpdater:
    """Select unfinished orders, build adapter targets and merge updates."""

    DEFAULT_TERMINAL_MARKERS = (
        "completed", "delivered", "received", "cancelled", "canceled", "closed", "finished",
    )

    def __init__(self, terminal_markers: tuple[str, ...] | None = None):
        self.terminal_markers = terminal_markers or self.DEFAULT_TERMINAL_MARKERS

    def is_terminal(self, status: str | None) -> bool:
        value = (status or "").strip().lower()
        return any(marker in value for marker in self.terminal_markers)

    def active_receipts(self, db: Session, account: Account) -> list[Receipt]:
        receipts = db.execute(
            select(Receipt).where(Receipt.account_id == account.id)
        ).scalars().all()
        return [receipt for receipt in receipts if not self.is_terminal(receipt.order_status)]

    @staticmethod
    def targets(receipts: list[Receipt]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for receipt in receipts:
            raw = receipt.raw if isinstance(receipt.raw, dict) else {}
            result[receipt.external_id] = raw.get("trackingUrl") or raw.get("tracking_url")
        return result

    @staticmethod
    def apply(receipts: list[Receipt], updates: dict[str, DeliveryUpdate]) -> int:
        changed = 0
        for receipt in receipts:
            update = updates.get(receipt.external_id)
            if update is None:
                continue
            dirty = False
            if update.status not in (None, "") and receipt.order_status != update.status:
                receipt.order_status = update.status
                dirty = True
            merged = list(dict.fromkeys([
                *(receipt.tracking_numbers or []),
                *(str(value) for value in update.tracking_numbers if value),
            ]))
            if merged != (receipt.tracking_numbers or []):
                receipt.tracking_numbers = merged
                dirty = True
            changed += int(dirty)
        return changed
