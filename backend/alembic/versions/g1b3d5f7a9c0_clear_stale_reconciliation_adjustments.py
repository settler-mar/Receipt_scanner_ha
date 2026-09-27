"""Remove stale legacy adjustments after provider components were parsed."""
from __future__ import annotations

import datetime as dt
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "g1b3d5f7a9c0"
down_revision: Union[str, Sequence[str], None] = "f0a2c4e6b8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("store_code", sa.String),
        sa.column("external_id", sa.String), sa.column("total_amount", sa.Numeric),
        sa.column("total_adjustment", sa.Numeric), sa.column("adjustment_reason", sa.String),
        sa.column("items_total", sa.Numeric), sa.column("components_total", sa.Numeric),
        sa.column("reconciled_total", sa.Numeric), sa.column("reconciliation_delta", sa.Numeric),
        sa.column("reconciliation_status", sa.String), sa.column("reconciliation_calculated_at", sa.DateTime()),
    )
    items = sa.table("receipt_items", sa.column("id", sa.String), sa.column("receipt_id", sa.String), sa.column("total_price", sa.Numeric))
    components = sa.table("receipt_components", sa.column("receipt_id", sa.String), sa.column("amount", sa.Numeric))

    # Once order-level AliExpress discounts are represented as components, the
    # old unexplained adjustment must be replaced by only the residual that is
    # still left after those components.
    ali_rows = bind.execute(sa.select(receipts.c.id, receipts.c.total_amount).where(receipts.c.store_code == "aliexpress")).all()
    for receipt_id, total_amount in ali_rows:
        item_sum = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(items.c.total_price), 0)).where(items.c.receipt_id == receipt_id)).scalar_one() or 0)
        component_sum = float(bind.execute(sa.select(sa.func.coalesce(sa.func.sum(components.c.amount), 0)).where(components.c.receipt_id == receipt_id)).scalar_one() or 0)
        residual = round(float(total_amount or 0) - item_sum - component_sum, 2)
        reason = "AliExpress: остаточная разница после выделения компонентов заказа" if abs(residual) >= 0.01 else None
        reconciled = round(item_sum + component_sum + residual, 2)
        bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
            total_adjustment=residual, adjustment_reason=reason,
            items_total=round(item_sum, 2), components_total=round(component_sum, 2),
            reconciled_total=reconciled, reconciliation_delta=round(reconciled - float(total_amount or 0), 2),
            reconciliation_status="open" if abs(reconciled - float(total_amount or 0)) >= 0.01 else "ok",
            reconciliation_calculated_at=dt.datetime.utcnow(),
        ))

    # The fiscal PDF for Vencon order 331681 contains one line for 2660 UAH;
    # remove the stale catalogue line left by the order-history parser.
    vencon_id = bind.execute(sa.select(receipts.c.id).where(
        (receipts.c.store_code == "vencon") & (receipts.c.external_id == "331681")
    )).scalar_one_or_none()
    if vencon_id:
        rows = bind.execute(sa.select(items.c.id, items.c.total_price).where(items.c.receipt_id == vencon_id)).all()
        for item_id, total_price in rows:
            if abs(float(total_price or 0) - 3799) < 0.01:
                bind.execute(sa.delete(items).where(items.c.id == item_id))
        bind.execute(sa.update(receipts).where(receipts.c.id == vencon_id).values(
            items_total=2660, components_total=0, reconciled_total=2660,
            reconciliation_delta=0, reconciliation_status="ok",
            reconciliation_calculated_at=dt.datetime.utcnow(),
        ))


def downgrade() -> None:
    pass
