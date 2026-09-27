"""Counters for the sidebar badges.

One cheap query set, one endpoint, so the navigation can say "there are three
things here waiting for you" without opening every page. Two tones:

  * `danger`  — something is **broken right now** and will stay broken until
    someone acts (a failing integration, a stuck job);
  * `warning` — nothing is broken, but there is a **decision to make** (receipts
    to review, duplicate groups, ambiguous shopping matches).

Everything counted here must be *resolvable*: a number has to disappear on its
own once the situation is handled. Historical events (a 503 from an AI provider
last night, a Telegram error) are journal material, never a badge.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.logs import active_problem_counts
from app.db import get_db
from app.models.models import DuplicateGroup, DuplicateStatus, Receipt
from app.models.scanner import InboxDocument, InboxStatus, ScanJob, ScanStatus
from app.models.shopping import (
    ShoppingListItem, ShoppingMatchCandidate, ShoppingMatchStatus,
)
from app.services import fiscal_queue, voice

router = APIRouter(prefix="/api/nav", tags=["nav"])


class NavBadge(BaseModel):
    to: str          # frontend route the badge belongs to
    count: int
    tone: str        # danger | warning
    hint: str


class NavBadges(BaseModel):
    items: list[NavBadge]


def _count(db: Session, model, *where) -> int:
    return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


@router.get("/badges", response_model=NavBadges)
def nav_badges(db: Session = Depends(get_db)):
    _, broken_accounts = active_problem_counts(db)

    # Инбокс — всё, что лежит на самой странице /inbox и ждёт действия: сканы в
    # работе, необработанные документы, неразобранные голосовые и очередь ДПС
    # (её строки теперь показываются там же).
    inbox = _count(db, ScanJob,
                   ScanJob.status.in_((ScanStatus.failed, ScanStatus.needs_review)),
                   ScanJob.archived_at.is_(None))
    inbox += _count(db, InboxDocument, InboxDocument.status == InboxStatus.unprocessed)
    inbox += voice.open_count(db)
    inbox += len(fiscal_queue.open_jobs(db))

    duplicates = _count(db, DuplicateGroup, DuplicateGroup.status.in_(
        (DuplicateStatus.pending, DuplicateStatus.needs_review)))

    # Список покупок: и «ймовірно куплено», и неоднозначные совпадения с чеком
    # ждут подтверждения пользователя.
    shopping = _count(db, ShoppingMatchCandidate,
                      ShoppingMatchCandidate.status == ShoppingMatchStatus.pending)
    shopping += _count(db, ShoppingListItem, ShoppingListItem.auto_matched.is_(True))

    receipt_problems = db.execute(
        select(func.count()).select_from(Receipt).where(
            Receipt.reconciliation_status == "open",
            Receipt.reconciliation_ignored_at.is_(None),
        )
    ).scalar_one()

    # Журнал сознательно без индикатора: он ничего не «держит» сам по себе —
    # каждая проблема живёт на своей странице, и её там видно.
    candidates = [
        ("/accounts", broken_accounts, "danger", "Інтеграцій зі збоєм"),
        ("/inbox", inbox, "warning", "Записів чекають на обробку"),
        ("/duplicates", duplicates, "warning", "Груп дублів на розгляд"),
        ("/shopping-list", shopping, "warning", "Позицій чекають підтвердження"),
        ("/receipts", receipt_problems, "warning", "Чеків з розбіжностями сум"),
    ]
    return NavBadges(items=[
        NavBadge(to=to, count=count, tone=tone, hint=hint)
        for to, count, tone, hint in candidates if count
    ])
