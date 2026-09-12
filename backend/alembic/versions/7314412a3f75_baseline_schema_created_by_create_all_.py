"""baseline (schema created by create_all up to 2026-08-29)

Revision ID: 7314412a3f75
Revises: 
Create Date: 2026-08-29 23:19:35.576924

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7314412a3f75'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op baseline.

    Every existing database up to 2026-08-29 was built by `Base.metadata.create_all`
    plus the ad-hoc `ensure_schema_upgrades()` ALTERs. This revision just marks
    that state as the migration starting point — existing DBs are stamped here
    (`alembic stamp head`), fresh DBs still get their tables from create_all in
    `app/main.py`. From here on, schema changes go through real migrations.
    """
    pass


def downgrade() -> None:
    pass
