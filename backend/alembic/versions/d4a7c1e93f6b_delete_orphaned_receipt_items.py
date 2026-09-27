"""delete receipt_items whose receipt no longer exists

Revision ID: d4a7c1e93f6b
Revises: c9e2f4a7b358
Create Date: 2026-09-21

Forensics (from a live audit, not a hypothesis): 5290 of 11371 `receipt_items`
rows (46%) referenced a `receipt_id` with no matching row in `receipts`, on
this database. The exact same 5290 were already present in the Aug 28 and
Sep 3 on-disk backups (`data/receipts.db.bak-*`, `data/receipts.*-reset.*`)
with an unchanged count in between — so this is one historical incident, not
an ongoing leak: every current receipt-deleting code path (accounts.py's
`delete_account`, receipts.py's `delete_receipt`, collector.py's excluded-
receipt cleanup, scan_jobs.py) goes through the ORM against a `Receipt` whose
`items`/`receipts` relationships already declare
`cascade="all, delete-orphan"`, so none of them can reproduce this. The
likely cause is a raw/out-of-band delete against `receipts` from outside the
app, before `app/db.py` started enforcing `PRAGMA foreign_keys=ON` (added
alongside this migration) — SQLite parses FK clauses but does not enforce
them per connection unless told to, so nothing stopped it at the time.

This migration is the cleanup: orphaned rows are permanently gone from the
receipts they document, carry no information no other table depends on (see
the docstring commit that introduced this file for the audit — no
`receipt_item_files`, `receipt_tags`, or `duplicate_groups.canonical_receipt_id`
rows referenced any orphaned id or item), and inflate every stat that counts
`receipt_items` without joining `receipts` (category stats, "Позицій з
категорією" on the Categories page). Not reversible — the deleted rows carry
no receipt to recover them from.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7c1e93f6b"
down_revision: Union[str, Sequence[str], None] = "c9e2f4a7b358"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "receipt_items" not in tables or "receipts" not in tables:
        return
    op.execute(
        "DELETE FROM receipt_items WHERE receipt_id NOT IN (SELECT id FROM receipts)"
    )


def downgrade() -> None:
    pass
