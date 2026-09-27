from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.registry import get_adapter
from app.db import get_db
from app.models.models import Store
from app.schemas.schemas import StoreClassOut, StoreOut
from app.services import store_classes

router = APIRouter(prefix="/api/stores", tags=["stores"])


@router.get("/classes", response_model=list[StoreClassOut])
def list_store_classes():
    """User-facing store classes (продуктовый / аптека / строительный / …) in
    display order. One dictionary for web, Telegram and MCP."""
    return store_classes.as_options()


@router.get("", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db)):
    stores = db.execute(select(Store).order_by(Store.name)).scalars().all()
    out = []
    for store in stores:
        item = StoreOut.model_validate(store)
        adapter = get_adapter(store.code)
        item.capabilities = adapter.capabilities
        item.login_field = adapter.login_field
        item.store_class = store_classes.class_for_store(store)
        item.store_class_name = store_classes.label(item.store_class)
        out.append(item)
    return out
