"""duplicate receipt candidates and merge log

Revision ID: e1d2c3b4a5f6
Revises: a1b2c3d4e5f6
Create Date: 2026-09-03
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1d2c3b4a5f6"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DUP_STATUS = sa.Enum("pending", "auto_merged", "needs_review", "rejected", "rolled_back", name="duplicatestatus")
_RUN_STATUS = sa.Enum("running", "success", "error", name="duplicaterunstatus")


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("duplicate_runs"):
        op.create_table(
            "duplicate_runs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("trigger", sa.String(), nullable=False, server_default="manual"),
            sa.Column("status", _RUN_STATUS, nullable=False, server_default="running"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scanned_receipts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_found", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidates_new", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_message", sa.String(), nullable=True),
        )
        op.create_index("ix_duplicate_runs_status", "duplicate_runs", ["status"])

    if not _has_table("duplicate_groups"):
        op.create_table(
            "duplicate_groups",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("status", _DUP_STATUS, nullable=False, server_default="pending"),
            sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reasons", sa.JSON(), nullable=True),
            sa.Column("receipt_ids", sa.JSON(), nullable=True),
            sa.Column("canonical_receipt_id", sa.String(), sa.ForeignKey("receipts.id"), nullable=True),
            sa.Column("created_by_run_id", sa.String(), sa.ForeignKey("duplicate_runs.id"), nullable=True),
            sa.Column("last_seen_run_id", sa.String(), sa.ForeignKey("duplicate_runs.id"), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("merge_log", sa.JSON(), nullable=True),
            sa.Column("rollback_snapshot", sa.JSON(), nullable=True),
            sa.UniqueConstraint("fingerprint", name="uq_duplicate_group_fingerprint"),
        )
        op.create_index("ix_duplicate_groups_fingerprint", "duplicate_groups", ["fingerprint"])
        op.create_index("ix_duplicate_groups_status_score", "duplicate_groups", ["status", "score"])

    if not _has_table("duplicate_candidates"):
        op.create_table(
            "duplicate_candidates",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("group_id", sa.String(), sa.ForeignKey("duplicate_groups.id"), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("status", _DUP_STATUS, nullable=False, server_default="pending"),
            sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reasons", sa.JSON(), nullable=True),
            sa.Column("receipt_ids", sa.JSON(), nullable=True),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("created_by_run_id", sa.String(), sa.ForeignKey("duplicate_runs.id"), nullable=True),
            sa.Column("last_seen_run_id", sa.String(), sa.ForeignKey("duplicate_runs.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("fingerprint", name="uq_duplicate_candidate_fingerprint"),
        )
        op.create_index("ix_duplicate_candidates_group_id", "duplicate_candidates", ["group_id"])
        op.create_index("ix_duplicate_candidates_fingerprint", "duplicate_candidates", ["fingerprint"])
        op.create_index("ix_duplicate_candidates_status_score", "duplicate_candidates", ["status", "score"])


def downgrade() -> None:
    for table in ("duplicate_candidates", "duplicate_groups", "duplicate_runs"):
        if _has_table(table):
            op.drop_table(table)

