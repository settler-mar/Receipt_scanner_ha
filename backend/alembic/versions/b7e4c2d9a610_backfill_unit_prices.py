"""backfill missing receipt item unit prices

Revision ID: b7e4c2d9a610
Revises: a3c7e5d19b04
Create Date: 2026-09-10

Some manual/OCR receipt lines contain a quantity and line total but no unit
price. Persist the safely derivable value so database-level analytics and
exports do not depend on API presentation fallbacks.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "b7e4c2d9a610"
down_revision: Union[str, Sequence[str], None] = "a3c7e5d19b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE receipt_items
        SET unit_price = ROUND(total_price / quantity, 2)
        WHERE unit_price IS NULL
          AND total_price IS NOT NULL
          AND quantity IS NOT NULL
          AND quantity > 0
        """
    )


def downgrade() -> None:
    # The derived values are valid receipt data and cannot be distinguished
    # from previously entered prices after the migration.
    pass
