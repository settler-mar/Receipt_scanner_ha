"""WOG/UPG bonuses stored in бали -> hryvnia

WOG's `BONUSES.INCREASE/DECREASE` and UPG's `AccruedBonus/WithdrawBonus` are
бали — hundredths of a hryvnia. Both adapters used to store them as-is, so every
collected receipt from those two stores carries bonus amounts 100× too large,
and the dashboard (which sums bonuses as money) reported them as roughly half of
all spend. The adapters now divide by 100; this rescales what was already saved.

Revision ID: a4b7c2e91d35
Revises: e2f3a4b5c6d7
Create Date: 2026-09-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b7c2e91d35"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORES = ("wog", "upg")


def _rescale(factor: str) -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE receipts "
            f"SET bonuses = bonuses {factor}, bonuses_used = bonuses_used {factor} "
            "WHERE store_code IN ('wog', 'upg')"
        )
    )


def upgrade() -> None:
    _rescale("/ 100.0")


def downgrade() -> None:
    _rescale("* 100.0")
