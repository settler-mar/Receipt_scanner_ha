"""Editable prompt catalogue and revision history."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services import prompt_catalog


def test_defaults_are_seeded_and_update_creates_revision():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        rows = prompt_catalog.list_prompts(db)
        assert len(rows) == len(prompt_catalog.DEFAULTS)
        original = next(row for row in rows if row.key == "language_variants")
        saved = prompt_catalog.update(db, original.key, original.content + " Дополнительное правило.", "Проверка")
        assert saved.version == 2
        revisions = prompt_catalog.history(db, original.key)
        assert [row.version for row in revisions] == [2, 1]
        assert revisions[0].note == "Проверка"
        assert prompt_catalog.get(db, original.key).endswith("Дополнительное правило.")
    finally:
        db.close()
