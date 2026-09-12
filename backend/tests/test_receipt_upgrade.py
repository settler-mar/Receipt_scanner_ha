import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.base import NormalizedItem, NormalizedReceipt
from app.api.inbox import _persist_receipt
from app.db import Base
from app.models.models import Account, AuthType, Store


def test_duplicate_fiscal_receipt_is_upgraded_when_full_version_arrives(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        store = Store(code="scanner", name="Сканер", auth_type=AuthType.manual)
        account = Account(store=store, label="scanner")
        db.add(account)
        db.commit()
        when = dt.datetime(2026, 7, 11, 8, 35, tzinfo=dt.timezone.utc)
        first = NormalizedReceipt(
            external_id="fiscal:2:1", purchased_at=when, total_amount=747.84,
            items=[NormalizedItem("Американо", 86)],
        )
        saved = _persist_receipt(db, account, first)
        full = NormalizedReceipt(
            external_id="fiscal:2:1", purchased_at=when, total_amount=747.84,
            shop_name="ГЛУСКО", shop_address="Бережани", payment_type="card",
            items=[NormalizedItem("Американо", 77.40), NormalizedItem("Хот дог", 59.50),
                   NormalizedItem("Хот дог Кантрі", 119), NormalizedItem("ГАЗ", 491.94)],
        )
        upgraded = _persist_receipt(db, account, full)
        assert upgraded.id == saved.id
        assert upgraded.shop_name == "ГЛУСКО"
        assert upgraded.shop_address == "Бережани"
        assert len(upgraded.items) == 4
        assert round(sum(float(item.total_price) for item in upgraded.items), 2) == 747.84
