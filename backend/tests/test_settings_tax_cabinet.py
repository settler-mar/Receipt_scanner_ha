"""Encrypted ДПС token persistence."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import models  # noqa: F401
from app.models.models import AppSetting
from app.services import tax_cabinet_settings as settings


def test_tax_token_is_encrypted_masked_and_clearable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    with session_factory() as db:
        assert settings.get_token(db) is None
        assert settings.token_hint(db) is None

        settings.set_token(db, "secret-token-1234")
        assert settings.get_token(db) == "secret-token-1234"
        assert settings.token_hint(db) == "••••1234"

        stored = db.get(AppSetting, settings.TOKEN_KEY).value
        assert stored["enc"] != "secret-token-1234"
        assert "secret-token-1234" not in str(stored)

        settings.set_token(db, None)
        assert settings.get_token(db) is None
        assert db.get(AppSetting, settings.TOKEN_KEY) is None
