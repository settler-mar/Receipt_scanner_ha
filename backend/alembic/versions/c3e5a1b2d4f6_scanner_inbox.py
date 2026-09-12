"""scan journal + document inbox tables (ROADMAP.md Крок 3)

Revision ID: c3e5a1b2d4f6
Revises: b2f1a7c9d3e4
Create Date: 2026-08-30

Chained after Крок 2 (b2f1a7c9d3e4) so `alembic upgrade head` stays linear. If
Крок 2's revision id shifts before merge, update `down_revision` to match.

Same skip-if-present guard as the earlier steps: on a fresh database
`Base.metadata.create_all` (app/main.py) builds these tables once
`app.models.scanner` is imported, so this migration only does real work on a
database that predates Крок 3.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e5a1b2d4f6"
down_revision: Union[str, Sequence[str], None] = "b2f1a7c9d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SCAN_STATUS = sa.Enum("pending", "parsed", "needs_review", "failed", name="scanstatus")
_INBOX_STATUS = sa.Enum("unprocessed", "done", name="inboxstatus")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("scan_jobs"):
        op.create_table(
            "scan_jobs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("source_filename", sa.String(), nullable=True),
            sa.Column("status", _SCAN_STATUS, nullable=False, server_default="pending"),
            sa.Column("method", sa.String(), nullable=True),
            sa.Column("confidence", sa.String(), nullable=True),
            sa.Column("receipt_id", sa.String(), nullable=True),
            sa.Column("raw_ocr", sa.JSON(), nullable=True),
            sa.Column("error", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_scan_jobs_status", "scan_jobs", ["status"])
        op.create_index("ix_scan_jobs_method", "scan_jobs", ["method"])

    if not _has_table("scan_samples"):
        op.create_table(
            "scan_samples",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("method", sa.String(), nullable=False),
            sa.Column("store_guess", sa.String(), nullable=True),
            sa.Column("ocr_text", sa.String(), nullable=True),
            sa.Column("structured", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table("inbox_documents"):
        op.create_table(
            "inbox_documents",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("storage", sa.String(), nullable=False, server_default="local"),
            sa.Column("drive_file_id", sa.String(), nullable=True),
            sa.Column("local_filename", sa.String(), nullable=True),
            sa.Column("original_filename", sa.String(), nullable=True),
            sa.Column("content_type", sa.String(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("ratio", sa.Float(), nullable=True),
            sa.Column("status", _INBOX_STATUS, nullable=False, server_default="unprocessed"),
            sa.Column("target_folder_id", sa.String(), nullable=True),
            sa.Column("final_name", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_inbox_documents_status", "inbox_documents", ["status"])


def downgrade() -> None:
    for table in ("inbox_documents", "scan_samples", "scan_jobs"):
        if _has_table(table):
            op.drop_table(table)
