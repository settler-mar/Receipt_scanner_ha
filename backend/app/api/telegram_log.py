"""Read-only operational journal for incoming Telegram messages."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import TelegramMessageLog

router = APIRouter(prefix="/api/telegram/messages", tags=["telegram"])


class TelegramMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    telegram_update_id: int
    message_id: int
    chat_id: int
    chat_type: str | None
    chat_title: str | None
    sender_id: int | None
    sender_username: str | None
    sender_display_name: str | None
    sender_is_bot: bool
    message_thread_id: int | None
    reply_to_message_id: int | None
    root_message_id: int
    conversation_key: str
    content_type: str
    text: str | None
    media_meta: dict
    authorized: bool
    status: str
    outcome: str | None
    error: str | None
    received_at: dt.datetime


class TelegramMessageList(BaseModel):
    total: int
    items: list[TelegramMessageOut]


@router.get("", response_model=TelegramMessageList)
def list_messages(
    chat_id: int | None = None,
    conversation_key: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = select(TelegramMessageLog)
    if chat_id is not None:
        query = query.where(TelegramMessageLog.chat_id == chat_id)
    if conversation_key:
        query = query.where(TelegramMessageLog.conversation_key == conversation_key)
    total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
    rows = db.execute(
        query.order_by(TelegramMessageLog.received_at.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return TelegramMessageList(total=total, items=rows)
