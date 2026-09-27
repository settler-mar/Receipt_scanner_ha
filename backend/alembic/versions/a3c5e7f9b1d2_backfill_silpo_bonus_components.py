"""Store Silpo bonus redemption as a signed receipt-level component."""
from __future__ import annotations

import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b1d2"
down_revision: Union[str, Sequence[str], None] = "f2b4d6e8a0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _raw(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def upgrade() -> None:
    bind = op.get_bind()
    receipts = sa.table(
        "receipts", sa.column("id", sa.String), sa.column("store_code", sa.String),
        sa.column("raw", sa.JSON), sa.column("total_adjustment", sa.Numeric),
        sa.column("adjustment_reason", sa.String),
    )
    components = sa.table(
        "receipt_components", sa.column("id", sa.String), sa.column("receipt_id", sa.String),
        sa.column("kind", sa.String), sa.column("code", sa.String), sa.column("label", sa.String),
        sa.column("amount", sa.Numeric), sa.column("currency", sa.String),
        sa.column("source", sa.String), sa.column("raw", sa.JSON),
    )
    rows = bind.execute(sa.select(receipts.c.id, receipts.c.raw).where(receipts.c.store_code == "silpo")).all()
    for receipt_id, raw_value in rows:
        raw = _raw(raw_value)
        rewards = [
            reward for reward in (raw.get("rewards") or [])
            if isinstance(reward, dict) and reward.get("rewardGroupCodeName") == "CN_REWARD_VOUCHERS"
        ]
        amount = round(-sum(float(reward.get("applyRewardAmount") or 0) for reward in rewards), 2)
        if abs(amount) < 0.01:
            continue
        exists = bind.execute(sa.select(sa.func.count()).select_from(components).where(components.c.receipt_id == receipt_id)).scalar_one()
        if exists:
            continue
        bind.execute(components.insert().values(
            id=str(uuid.uuid4()), receipt_id=receipt_id, kind="discount", code="bonuses_used",
            label="Використані балобонуси", amount=amount, currency="UAH", source="silpo.rewards", raw={"rewards": rewards},
        ))
        bind.execute(sa.update(receipts).where(receipts.c.id == receipt_id).values(
            total_adjustment=0, adjustment_reason=None,
        ))


def downgrade() -> None:
    pass
