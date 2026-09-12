from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.registry import get_adapter
from app.db import get_db
from app.models.models import Store
from app.schemas.schemas import StoreOut

router = APIRouter(prefix="/api/stores", tags=["stores"])


@router.get("", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db)):
    stores = db.execute(select(Store).order_by(Store.name)).scalars().all()
    out = []
    for store in stores:
        item = StoreOut.model_validate(store)
        item.capabilities = get_adapter(store.code).capabilities
        out.append(item)
    return out
