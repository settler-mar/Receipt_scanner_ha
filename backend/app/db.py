from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
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
        },
        "receipts": {"exchange_rate_to_uah": "NUMERIC"},
        "accounts": {
            "fuel_prices_checked_at": "DATETIME",
            "fuel_discount_allowance": "NUMERIC",
            "fuel_discount_allowance_unit": "VARCHAR",
            "fuel_discount_allowance_resets_at": "DATETIME",
        },
        "fiscal_queue_jobs": {"scan_job_id": "VARCHAR"},
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
