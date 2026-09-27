from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import BankTransactionRule
from app.schemas.schemas import (
    BankTransactionRuleApplyResult,
    BankTransactionRuleCreate,
    BankTransactionRuleOut,
    BankTransactionRuleUpdate,
)
from app.services.bank_transaction_rules import apply_rules

router = APIRouter(prefix="/api/bank-transaction-rules", tags=["bank-transactions"])


@router.get("", response_model=list[BankTransactionRuleOut])
def list_rules(db: Session = Depends(get_db)):
    return db.execute(
        select(BankTransactionRule).order_by(BankTransactionRule.priority)
    ).scalars().all()


@router.post("", response_model=BankTransactionRuleOut, status_code=201)
def create_rule(payload: BankTransactionRuleCreate, db: Session = Depends(get_db)):
    rule = BankTransactionRule(**payload.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.patch("/{rule_id}", response_model=BankTransactionRuleOut)
def update_rule(rule_id: str, payload: BankTransactionRuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(BankTransactionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не знайдено")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: str, db: Session = Depends(get_db)):
    rule = db.get(BankTransactionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило не знайдено")
    db.delete(rule)
    db.commit()


@router.post("/apply", response_model=BankTransactionRuleApplyResult)
def apply_now(db: Session = Depends(get_db)):
    """Retroactively re-run every rule against existing transactions — for
    testing a rule against history right after creating it, not just future
    ingests (which also call apply_rules automatically)."""
    return BankTransactionRuleApplyResult(updated=apply_rules(db))
