"""Merge the existing schema branches after reconciliation snapshot changes."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "h2c4e6a8b0d1"
down_revision: Union[str, Sequence[str], None] = ("b8d1f3a5c7e9", "g1b3d5f7a9c0")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
