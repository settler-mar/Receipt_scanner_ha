from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import as_utc
from app.db import get_db
from app.models.models import Account, Receipt, ReceiptItem, Store
from app.schemas.schemas import WarrantyProductOut

router = APIRouter(prefix="/api/warranties", tags=["warranties"])


@router.get("", response_model=list[WarrantyProductOut])
def list_warranty_products(db: Session = Depends(get_db)):
    rows = db.execute(
        select(ReceiptItem, Receipt, Account, Store)
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .join(Account, Account.id == Receipt.account_id)
        .join(Store, Store.id == Account.store_id)
        .where(ReceiptItem.has_warranty.is_(True))
        .options(selectinload(ReceiptItem.files))
    ).all()
    now = dt.datetime.now(dt.timezone.utc)
    products = []
    for item, receipt, account, store in rows:
        expires_at = as_utc(item.warranty_expires_at)
        quantity = float(item.quantity) or 1.0
        file_urls = [
            f"/api/receipts/{receipt.id}/items/{item.id}/files/{f.id}" for f in item.files
        ]
        products.append(
            WarrantyProductOut(
                item_id=item.id,
                receipt_id=receipt.id,
                name=item.name,
                store_code=receipt.store_code,
                store_name=store.name,
                account_label=account.label,
                purchased_at=as_utc(receipt.purchased_at),
                purchase_price=float(item.total_price) / quantity,
                warranty_text=item.warranty_text,
                warranty_expires_at=expires_at,
                expired=bool(expires_at and expires_at < now),
                product_url=item.product_url,
                # Hand-attached photos win over the store's catalogue picture:
                # if the талон/product was photographed by hand, that is the
                # copy worth showing on the warranty card.
                image_url=(file_urls[0] if file_urls else item.product_image_url),
                file_urls=file_urls,
            )
        )
    products.sort(
        key=lambda product: (
            product.expired,
            product.warranty_expires_at or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
        )
    )
    return products
