"""fiscal receipt processing queue

Revision ID: a1b2c3d4e5f6
Revises: f8a9b0c1d2e3
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

def upgrade():
    inspector=sa.inspect(op.get_bind())
    if "fiscal_queue_jobs" in inspector.get_table_names():
        columns={c["name"] for c in inspector.get_columns("fiscal_queue_jobs")}
        if "scan_job_id" not in columns: op.add_column("fiscal_queue_jobs",sa.Column("scan_job_id",sa.String(),nullable=True))
        return
    op.create_table("fiscal_queue_jobs",
        sa.Column("id",sa.String(),primary_key=True), sa.Column("url",sa.String(),nullable=False),
        sa.Column("dedup_key",sa.String(),nullable=False), sa.Column("source",sa.String(),nullable=False),
        sa.Column("telegram_chat_id",sa.String(),nullable=True),
        sa.Column("scan_job_id",sa.String(),nullable=True),
        sa.Column("status",sa.Enum("queued","processing","captcha","imported","failed",name="fiscalqueuestatus"),nullable=False),
        sa.Column("attempts",sa.Integer(),nullable=False), sa.Column("receipt_id",sa.String(),nullable=True),
        sa.Column("error",sa.String(),nullable=True), sa.Column("session_id",sa.String(),nullable=True),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False), sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False))
    op.create_index("ix_fiscal_queue_jobs_dedup_key","fiscal_queue_jobs",["dedup_key"],unique=True)
    op.create_index("ix_fiscal_queue_jobs_status","fiscal_queue_jobs",["status"])

def downgrade():
    op.drop_index("ix_fiscal_queue_jobs_status",table_name="fiscal_queue_jobs")
    op.drop_index("ix_fiscal_queue_jobs_dedup_key",table_name="fiscal_queue_jobs")
    op.drop_table("fiscal_queue_jobs")
