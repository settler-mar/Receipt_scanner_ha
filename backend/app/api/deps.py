from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import Account

DbDep = Depends(get_db)


def get_account_or_404(account_id: str, db: Session = DbDep) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


__all__ = ["get_db", "get_account_or_404", "DbDep"]
