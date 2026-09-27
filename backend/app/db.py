from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)

if settings.database_url.startswith("sqlite"):
    # SQLite parses FOREIGN KEY clauses but does not enforce them unless told
    # to, per connection. Every relationship that should cascade already does
    # so at the ORM level (Account/Receipt/ReceiptItem all declare
    # cascade="all, delete-orphan") — this is a second, DB-level backstop so
    # that any *non-ORM* delete (a raw script, a future migration, a manual
    # `sqlite3` session) fails loudly instead of silently leaving orphaned
    # rows behind, the way ~5290 receipt_items did historically before this
    # was added (cleaned up by migration; see its docstring for the forensics).
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema_upgrades() -> None:
    """Apply the small additive upgrades needed by existing SQLite databases."""
    upgrades = {
        "receipt_items": {
            "product_url": "VARCHAR",
            "product_image_url": "VARCHAR",
            "product_image_source_url": "VARCHAR",
            "has_warranty": "BOOLEAN",
            "warranty_text": "VARCHAR",
            "warranty_expires_at": "DATETIME",
            "warranty_checked_at": "DATETIME",
            "product_code": "VARCHAR",
        },
        "receipts": {
            "exchange_rate_to_uah": "NUMERIC",
            "reconciliation_ignored_at": "DATETIME",
            "total_adjustment": "NUMERIC",
            "adjustment_reason": "VARCHAR",
        },
        "accounts": {
            "fuel_prices_checked_at": "DATETIME",
            "fuel_discount_allowance": "NUMERIC",
            "fuel_discount_allowance_unit": "VARCHAR",
            "fuel_discount_allowance_resets_at": "DATETIME",
        },
        "fiscal_queue_jobs": {"scan_job_id": "VARCHAR"},
        "shopping_list_items": {"tag": "VARCHAR"},
        "collection_runs": {"progress_current": "INTEGER", "progress_total": "INTEGER"},
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, additions in upgrades.items():
            if table not in inspector.get_table_names():
                continue
            columns = {column["name"] for column in inspector.get_columns(table)}
            for name, sql_type in additions.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))


def run_migrations() -> None:
    """Apply Alembic migrations up to head.

    Coexists with `create_all` for now: `create_all` still builds a fresh DB's
    tables, this then walks it forward through any migrations (the 2026-08-29
    baseline is a no-op, so an existing un-stamped DB is simply stamped and
    carried forward). New schema changes are migrations from here on — see
    backend/alembic/.
    """
    from alembic import command
    from alembic.config import Config

    from app.core.config import BASE_DIR

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
