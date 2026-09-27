"""Telegram bot: receipt ingestion and a small chat facade over MCP features."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import html
import ipaddress
import io
import json
import logging
import re
import time
import uuid
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import decrypt, encrypt
from app.core.config import settings
from app.core.time import KYIV, as_utc
from app.db import SessionLocal
from app.models.models import AppSetting, TelegramMessageLog
from app.services.ai import AiConfigError

logger = logging.getLogger(__name__)

CONFIG_KEY = "telegram_config"
TOKEN_KEY = "telegram_bot_token"
# Kept only to delete/hide secrets left by the pre-approval implementation.
LEGACY_AUTH_KEY = "telegram_auth_code"
_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
_MAX_DOWNLOAD = 40 * 1024 * 1024
_HTML_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
# How many captured shopping batches stay undoable.
_CAPTURE_BATCH_MEMORY = 20

# Telegram shows these in the slash-command picker. Keep this list deliberately
# short: the remaining commands are still supported by the dispatcher, but are
# either contextual (/buy, /trip_done, /cancel) or easier to use in plain
# language (/add, /find). This keeps the picker useful instead of turning it
# into a second, noisy help page.
_BOT_COMMANDS = [
    {"command": "start", "description": "Справка и возможности бота"},
    {"command": "trip", "description": "Чеклист и выбор магазина"},
    {"command": "receipts", "description": "Показать последние чеки"},
    {"command": "spending", "description": "Показать расходы"},
]
_commands_configured_token: str | None = None


class TelegramConfig(BaseModel):
    enabled: bool = False
    # Address the links in bot messages point at. `settings.base_url` is the
    # backend's own idea of where it runs, which on a laptop install is often
    # localhost — useless in a chat opened from a phone. Empty = fall back to it.
    public_url: str = ""
    authorized_chat_ids: list[int] = Field(default_factory=list)
    authorized_chats: list["AuthorizedChat"] = Field(default_factory=list)
    pending_chats: list["PendingChat"] = Field(default_factory=list)
    pending_fiscal_urls: dict[str, str] = Field(default_factory=dict)
    # Read a plain sentence ("додай чек ...", "додай в список ...") with AI and
    # turn it into a receipt draft or shopping-list entries. Off means the bot
    # answers such a message with its usual help text, as before.
    capture_enabled: bool = True
    # batch id -> shopping-list item ids added by one captured message, so the
    # "Скасувати" button under the answer can take them back out. Only the last
    # few are kept; an id that aged out just reports it can no longer undo.
    capture_batches: dict[str, list[str]] = Field(default_factory=dict)
    # chat id -> scan job the next photo should be appended to. A long receipt is
    # photographed in overlapping frames; this is what "➕ Додати ще фото" arms.
    pending_series: dict[str, str] = Field(default_factory=dict)


class PendingChat(BaseModel):
    chat_id: int
    username: str | None = None
    display_name: str | None = None
    requested_at: str


class AuthorizedChat(BaseModel):
    chat_id: int
    username: str | None = None
    display_name: str | None = None
    approved_at: str | None = None


class TelegramSettingsOut(BaseModel):
    config: TelegramConfig
    token_hint: str | None = None
    bot_username: str | None = None


def _secret(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    if row is None or not row.value:
        return None
    try:
        return decrypt(str(row.value.get("encrypted", "")))
    except Exception:
        return None


def _set_secret(db: Session, key: str, value: str | None) -> None:
    row = db.get(AppSetting, key)
    if not value:
        if row is not None:
            db.delete(row)
    else:
        payload = {"encrypted": encrypt(value.strip())}
        if row is None:
            db.add(AppSetting(key=key, value=payload))
        else:
            row.value = payload
    db.commit()


def load_config(db: Session) -> TelegramConfig:
    row = db.get(AppSetting, CONFIG_KEY)
    try:
        config = TelegramConfig.model_validate(row.value if row else {})
        known = {item.chat_id for item in config.authorized_chats}
        config.authorized_chats.extend(
            AuthorizedChat(chat_id=chat_id) for chat_id in config.authorized_chat_ids if chat_id not in known
        )
        return config
    except Exception:
        return TelegramConfig()


def save_config(db: Session, config: TelegramConfig) -> TelegramConfig:
    # Stable order makes settings responses and tests deterministic.
    config.authorized_chat_ids = sorted(set(config.authorized_chat_ids))
    unique_authorized = {item.chat_id: item for item in config.authorized_chats}
    for chat_id in config.authorized_chat_ids:
        unique_authorized.setdefault(chat_id, AuthorizedChat(chat_id=chat_id))
    config.authorized_chats = sorted(unique_authorized.values(), key=lambda item: item.chat_id)
    authorized = set(config.authorized_chat_ids)
    unique_pending = {item.chat_id: item for item in config.pending_chats if item.chat_id not in authorized}
    config.pending_chats = sorted(unique_pending.values(), key=lambda item: item.requested_at)
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=config.model_dump()))
    else:
        row.value = config.model_dump()
    db.commit()
    return config


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def app_url(suffix: str = "", *, db: Session | None = None) -> str:
    """Base address for links the bot sends, plus an optional `#/…` suffix.

    Read fresh on every message so a change in Settings takes effect without a
    restart. Returns "" when only a loopback address is known: the recipient
    reads the message on a phone, where localhost is the phone itself — a link
    there is worse than no link, and Telegram rejects it outright in a button.
    """
    try:
        if db is not None:
            configured = load_config(db).public_url.strip()
        else:
            with SessionLocal() as own:
                configured = load_config(own).public_url.strip()
    except Exception:  # noqa: BLE001 — never let a link lookup break a reply
        logger.debug("could not read the configured public URL", exc_info=True)
        configured = ""
    base = (configured or settings.base_url or "").rstrip("/")
    if not base or urlparse(base).hostname in _LOOPBACK:
        return ""
    return f"{base}{suffix}"


def token_hint(db: Session) -> str | None:
    value = _secret(db, TOKEN_KEY)
    return f"••••{value[-6:]}" if value else None


async def check_token(token: str) -> tuple[bool, str | None, str]:
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        data = response.json()
        if response.is_success and data.get("ok"):
            username = data.get("result", {}).get("username")
            return True, username, "Підключено"
        return False, None, str(data.get("description") or "Telegram відхилив токен")
    except Exception as exc:  # noqa: BLE001
        return False, None, str(exc)


async def _api(token: str, method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=65) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {})
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or method)
    return data


async def _ensure_bot_commands(token: str) -> None:
    """Install the slash-command menu once per bot token.

    Telegram keeps commands server-side, so doing this on polling startup also
    fixes menus for an already-running bot after a deploy or token change.
    """
    global _commands_configured_token
    if _commands_configured_token == token:
        return
    try:
        await _api(token, "setMyCommands", {"commands": _BOT_COMMANDS})
    except Exception:  # noqa: BLE001 — a temporary Telegram outage must not stop polling
        logger.warning("could not configure Telegram command menu", exc_info=True)
        return
    _commands_configured_token = token


@dataclasses.dataclass
class BotReply:
    """A bot answer that may carry an inline keyboard of follow-up actions."""

    text: str
    keyboard: list[list[dict]] | None = None
    photo: bytes | None = None


async def _send(
    token: str,
    chat_id: int,
    text: str,
    *,
    reply_to: int | None = None,
    thread_id: int | None = None,
    keyboard: list[list[dict]] | None = None,
) -> None:
    payload: dict = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    if reply_to is not None:
        # Thread every answer under the message it responds to so a chat with
        # several receipts stays legible.
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    await _api(token, "sendMessage", payload)


async def _send_photo(
    token: str,
    chat_id: int,
    photo: bytes,
    caption: str,
    *,
    reply_to: int | None = None,
    thread_id: int | None = None,
    keyboard: list[list[dict]] | None = None,
) -> None:
    """Send a generated chart through Telegram's multipart upload endpoint."""
    data = {"chat_id": str(chat_id), "caption": caption[:1024], "parse_mode": "HTML"}
    if thread_id is not None:
        data["message_thread_id"] = str(thread_id)
    if reply_to is not None:
        data["reply_parameters"] = json.dumps({
            "message_id": reply_to,
            "allow_sending_without_reply": True,
        })
    if keyboard:
        data["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
    async with httpx.AsyncClient(timeout=65) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=data,
            files={"photo": ("spending.png", photo, "image/png")},
        )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("description") or "sendPhoto")


async def _edit_message(token: str, chat_id: int, message_id: int, text: str,
                        keyboard: list[list[dict]] | None = None) -> None:
    """Replace a Telegram checklist message in place.

    Shopping-trip callbacks are deliberately rendered as one evolving message:
    all participants keep looking at the same checklist instead of receiving a
    new bot message for every checkbox or store choice.
    """
    await _api(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard or []},
    })


#: Button actions slow enough that silence reads as a dead button. The text is
#: what the user sees while the work runs; the result follows in the same thread.
_STARTED_LINES = {
    ("rev", "rescan"): "🔄 Пересканую…",
    ("rev", "ai"): "🤖 Розпізнаю через AI…",
    ("rev", "up"): "⚡ Пробую сильнішу AI…",
    ("rev", "save"): "💾 Зберігаю чернетку…",
    ("ser", "one"): "🧩 Склеюю кадри в один чек…",
    ("ser", "many"): "🧾 Розбираю кадри як окремі чеки…",
    ("voi", "stt"): "🎧 Розпізнаю запис…",
    ("voi", "lang"): "🎧 Розпізнаю запис іншою мовою…",
    ("tri", "categorize"): "🧠 Розподіляю товари за категоріями…",
}


def _started_line(prefix: str, action: str) -> str:
    """What to say before a slow button action, or "" for an instant one."""
    return _STARTED_LINES.get((prefix, action.removesuffix("x")), "")


async def _ack(token: str, chat_id: int, reply_to: int | None,
               thread_id: int | None = None,
               text: str = "📥 Чек отримано, розпізнаю…") -> None:
    """Confirm the receipt arrived, before the slow part starts.

    Recognition runs OCR, a ДПС lookup and possibly a vision model, which can
    take tens of seconds — long enough for silence to read as the bot ignoring
    the message and invite a second copy of the same photo.
    """
    try:
        await _send(token, chat_id, text, reply_to=reply_to, thread_id=thread_id)
    except Exception:  # noqa: BLE001 — the real answer still follows
        logger.debug("could not send the Telegram acknowledgement", exc_info=True)


def _authorized(db: Session, chat_id: int) -> bool:
    return chat_id in load_config(db).authorized_chat_ids


def request_approval(db: Session, message: dict) -> bool:
    """Add an unknown chat to the approval queue. Returns True only when new."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return False
    cfg = load_config(db)
    if chat_id in cfg.authorized_chat_ids or any(row.chat_id == chat_id for row in cfg.pending_chats):
        return False
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
    cfg.pending_chats.append(PendingChat(
        chat_id=chat_id,
        username=sender.get("username"),
        display_name=name or chat.get("title"),
        requested_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    ))
    save_config(db, cfg)
    return True


def update_authorized_identity(db: Session, message: dict) -> None:
    """Refresh Telegram profile fields opportunistically on every message."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return
    cfg = load_config(db)
    if chat_id not in cfg.authorized_chat_ids:
        return
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
    row = next((item for item in cfg.authorized_chats if item.chat_id == chat_id), None)
    if row is None:
        row = AuthorizedChat(chat_id=chat_id)
        cfg.authorized_chats.append(row)
    changed = False
    for field, value in (("username", sender.get("username")),
                         ("display_name", name or chat.get("title"))):
        if value and getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    if changed:
        save_config(db, cfg)


def _message_content(message: dict) -> tuple[str, dict]:
    """Classify content and retain only useful, non-secret media metadata."""
    for key in ("photo", "document", "voice", "audio", "video_note", "video", "sticker", "location", "contact"):
        value = message.get(key)
        if not value:
            continue
        media = value[-1] if key == "photo" and isinstance(value, list) else value
        meta = {
            field: media.get(field)
            for field in ("file_name", "mime_type", "file_size", "duration", "width", "height")
            if isinstance(media, dict) and media.get(field) is not None
        }
        return key, meta
    return ("text" if message.get("text") else "other"), {}


def record_incoming_message(db: Session, update: dict, *, authorized: bool) -> TelegramMessageLog | None:
    """Persist one incoming Telegram message and its conversation branch."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    update_id, message_id, chat_id = update.get("update_id"), message.get("message_id"), chat.get("id")
    if not all(isinstance(value, int) for value in (update_id, message_id, chat_id)):
        return None
    existing = db.execute(
        select(TelegramMessageLog).where(TelegramMessageLog.telegram_update_id == update_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    reply = message.get("reply_to_message") or {}
    reply_to = reply.get("message_id") if isinstance(reply.get("message_id"), int) else None
    # Telegram includes one level of reply ancestry. A user often replies to a
    # bot answer which itself replied to the original user message; use that
    # original message to keep the whole exchange in one branch.
    replied_parent = reply.get("reply_to_message") or {}
    ancestry_id = replied_parent.get("message_id") \
        if isinstance(replied_parent.get("message_id"), int) else None
    thread_id = message.get("message_thread_id")
    if not isinstance(thread_id, int):
        thread_id = reply.get("message_thread_id") if isinstance(reply.get("message_thread_id"), int) else None
    parent = None
    for candidate_id in (reply_to, ancestry_id):
        if candidate_id is None:
            continue
        parent = db.execute(select(TelegramMessageLog).where(
            TelegramMessageLog.chat_id == chat_id,
            TelegramMessageLog.message_id == candidate_id,
        )).scalar_one_or_none()
        if parent is not None:
            break
    root_message_id = thread_id or (parent.root_message_id if parent else ancestry_id or reply_to) or message_id
    conversation_key = (
        f"{chat_id}:topic:{thread_id}" if thread_id is not None
        else f"{chat_id}:reply:{root_message_id}"
    )
    content_type, media_meta = _message_content(message)
    display_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
    sent_at = dt.datetime.fromtimestamp(message.get("date"), dt.timezone.utc) \
        if isinstance(message.get("date"), int) else dt.datetime.now(dt.timezone.utc)
    row = TelegramMessageLog(
        telegram_update_id=update_id, message_id=message_id, chat_id=chat_id,
        chat_type=chat.get("type"), chat_title=chat.get("title"),
        sender_id=sender.get("id") if isinstance(sender.get("id"), int) else None,
        sender_username=sender.get("username"), sender_display_name=display_name or None,
        sender_is_bot=bool(sender.get("is_bot")), message_thread_id=thread_id,
        reply_to_message_id=reply_to, root_message_id=root_message_id,
        conversation_key=conversation_key, content_type=content_type,
        text=(message.get("text") or message.get("caption") or None), media_meta=media_meta,
        authorized=authorized, status="received", received_at=sent_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def finish_incoming_message(log_id: str | None, *, status: str, outcome: str | None = None,
                            error: str | None = None) -> None:
    if not log_id:
        return
    with SessionLocal() as db:
        row = db.get(TelegramMessageLog, log_id)
        if row is None:
            return
        row.status = status
        row.outcome = outcome[:1000] if outcome else None
        row.error = error[:2000] if error else None
        db.commit()


async def _download_telegram_file(token: str, file_id: str) -> tuple[bytes, str]:
    info = await _api(token, "getFile", {"file_id": file_id})
    path = info["result"]["file_path"]
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
    response.raise_for_status()
    if len(response.content) > _MAX_DOWNLOAD:
        raise ValueError("Файл більший за 40 МБ")
    return response.content, response.headers.get("content-type", "image/jpeg")


def _review_keyboard(scan_job_id: str, *, saved: bool = False,
                     rescan: bool = True, save: bool = True,
                     next_ai: tuple[str, str, str] | None = None) -> list[list[dict]]:
    """Actions offered under a scan result.

    A saved receipt keeps the rescan and delete buttons — a parse can look
    complete and still be wrong — with the `x` flag marking that they act on the
    stored receipt too. Only "save the draft" is dropped, having nothing to act on.

    `rescan=False` is for a draft that came from a typed or dictated message:
    there is no image to run through the pipeline again, so offering it would
    only produce an error.

    `save=False` is for a photo nothing came out of: there is no draft to accept,
    but rerunning it through vision, attaching the next frame or throwing the
    attempt away are exactly what the user needs next — a dead-end message with
    no buttons was the worst place to leave them.

    `next_ai` is the step of the OCR route after the one this photo already
    spent — offered by name, because "stronger" is only the wording: the real
    order is the one the user set in AI → Маршрути задач. Absent for a result no
    model read, and once the route has no step left.
    """
    flag = "x" if saved else ""
    rows = []
    if not saved and save:
        rows.append([{"text": "✅ Зберегти як є", "callback_data": f"rev:save:{scan_job_id}"}])
    if rescan:
        rows.append([
            {"text": "🔄 Пересканувати", "callback_data": f"rev:rescan{flag}:{scan_job_id}"},
            {"text": "🤖 Розпізнати AI", "callback_data": f"rev:ai{flag}:{scan_job_id}"},
        ])
        if next_ai:
            _service, model, label = next_ai
            rows.append([{"text": f"⚡ Сильніша AI → {label} / {model}",
                          "callback_data": f"rev:up{flag}:{scan_job_id}"}])
    if not saved:
        # A receipt longer than the frame is shot in parts; this is how the next
        # part is attached to the draft standing here.
        rows.append([{"text": "➕ Додати ще фото цього чека",
                      "callback_data": f"ser:add:{scan_job_id}"}])
    rows.append([{
        "text": "🗑 Видалити чек" if saved else "🗑 Видалити",
        "callback_data": f"rev:del{flag}:{scan_job_id}",
    }])
    # Telegram rejects the whole message if a URL button isn't publicly valid,
    # so the row is simply dropped when no reachable address is configured.
    inbox_url = app_url("/#/inbox")
    if inbox_url:
        rows.append([{"text": "✏️ Відкрити в Інбоксі", "url": inbox_url}])
    return rows


def _shopping_capture_reply(db: Session, rows: list) -> BotReply:
    """Render a shopping capture and keep the same one-tap undo as text input."""
    batch = _remember_capture_batch(db, [row.id for row in rows])
    lines = [f"🛒 Додано до списку ({len(rows)}):"]
    for row in rows:
        qty = (f" — {float(row.qty):g}{(' ' + row.unit) if row.unit else ''}"
               if row.qty and float(row.qty) != 1 else "")
        note = f" ({row.note})" if row.note else ""
        state = {"new": "🆕", "changed": "✏️", "existing": "↩️"}.get(
            getattr(row, "_shopping_add_state", "new"), "🆕")
        lines.append(f"{state} {row.raw_text}{qty}{note}")
    return BotReply("\n".join(lines), keyboard=[
        [{"text": "↩️ Скасувати", "callback_data": f"cap:undo:{batch}"}],
    ])


# --------------------------------------------------------------------------- #
# A receipt photographed in several overlapping frames
# --------------------------------------------------------------------------- #
# Telegram delivers an album as separate updates that only share a
# `media_group_id`, with no "that was the last one" marker. Frames are collected
# per group and processed once the group has been quiet for a moment.
_ALBUM_QUIET_S = 2.5
_albums: dict[str, dict] = {}


def _album_collect(media_group_id: str, file_id: str) -> bool:
    """Buffer one frame of an album. True when this frame opened the group.

    Telegram delivers an album as separate updates sharing a `media_group_id`,
    with no marker on the last one — and the polling loop handles updates one at
    a time, so whoever waits for the rest must do it *off* that loop or the rest
    can never arrive. Hence: buffer here, work in a background task.
    """
    group = _albums.setdefault(media_group_id, {"parts": [], "claimed": False})
    group["parts"].append(file_id)
    group["deadline"] = time.monotonic() + _ALBUM_QUIET_S
    if group["claimed"]:
        return False
    group["claimed"] = True
    return True


async def _album_worker(token: str, media_group_id: str, *, chat_id: int,
                        reply_to: int | None, thread_id: int | None) -> None:
    """Wait for the album to stop growing, then handle it as one batch."""
    try:
        while True:
            group = _albums.get(media_group_id)
            if group is None:
                return
            wait = group["deadline"] - time.monotonic()
            if wait <= 0:
                break
            await asyncio.sleep(min(wait, 0.5))
        file_ids = list(_albums.pop(media_group_id, {}).get("parts") or [])
        if not file_ids:
            return
        parts = []
        for index, file_id in enumerate(file_ids, start=1):
            data, media = await _download_telegram_file(token, file_id)
            parts.append((data, media, f"Telegram · фото {index} з {len(file_ids)}"))
        reply = await _scan_batch_parts(parts, chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001 — a background task swallows nothing
        logger.exception("Telegram album handling failed")
        reply = BotReply(f"Не вдалося обробити серію фото: {exc}")
    if reply.photo:
        await _send_photo(token, chat_id, reply.photo, html.escape(reply.text),
                          reply_to=reply_to, thread_id=thread_id, keyboard=reply.keyboard)
    else:
        await _send(token, chat_id, html.escape(reply.text), reply_to=reply_to,
                    thread_id=thread_id, keyboard=reply.keyboard)


def _arm_pending_series(chat_id: int, job_id: str) -> None:
    """Remember that the next photo from this chat continues `job_id`."""
    with SessionLocal() as db:
        cfg = load_config(db)
        cfg.pending_series[str(chat_id)] = job_id
        save_config(db, cfg)


def _take_pending_series(chat_id: int) -> str | None:
    """Consume the armed series, if any — one photo joins one series."""
    with SessionLocal() as db:
        cfg = load_config(db)
        job_id = cfg.pending_series.pop(str(chat_id), None)
        if job_id is not None:
            save_config(db, cfg)
        return job_id


async def _scan_batch_parts(parts: list[tuple[bytes, str, str | None]], *,
                            chat_id: int | None = None, job_id: str | None = None,
                            choice: str | None = None) -> "BotReply":
    """Photos sent together: one long receipt, or several separate ones."""
    from app.services import series_scan

    with SessionLocal() as db:
        job = series_scan.open_series(db, job_id) if job_id else None
        if job_id and job is None:
            return BotReply("Цей чек уже збережено або видалено — надішліть фото заново.")
        try:
            outcome = await series_scan.scan_batch(db, parts, job=job, choice=choice)
        except ValueError as exc:
            return BotReply(f"Не вдалося обробити серію: {exc}")

    if outcome.needs_choice:
        # The QR codes did not settle it, and guessing costs a model call per
        # frame plus receipts to clean up afterwards — so ask.
        return BotReply(
            f"📸 Отримано {len(parts)} фото. {outcome.note}",
            keyboard=[[
                {"text": "🧩 Один чек", "callback_data": f"ser:one:{outcome.scan_job_id}"},
                {"text": "🧾 Різні чеки", "callback_data": f"ser:many:{outcome.scan_job_id}"},
            ]],
        )
    return await _describe_batch(outcome, len(parts))


async def _describe_batch(outcome, sent: int) -> "BotReply":
    """One answer for the whole batch, whichever way it was read."""
    if outcome.kind == "separate":
        lines = [f"🧾 {outcome.note}.", ""]
        for index, result in enumerate(outcome.results, start=1):
            if result.receipt_id:
                lines.append(f"{index}. ✅ чек збережено"
                             + (" (дубль)" if result.duplicate else ""))
            elif result.needs_review:
                lines.append(f"{index}. 🟡 розпізнано частково — перевірте в Інбоксі")
            else:
                lines.append(f"{index}. ⚠️ не розпізнано")
        keyboard = None
        inbox_url = app_url("/#/inbox")
        if inbox_url:
            keyboard = [[{"text": "✏️ Відкрити в Інбоксі", "url": inbox_url}]]
        return BotReply("\n".join(lines), keyboard=keyboard)
    return await _describe_series(outcome.results[0], sent)


async def _resolve_series_choice(job_id: str, choice: str) -> "BotReply":
    """The answer to "one receipt or several" — recognise it that way."""
    from app.services import series_scan

    with SessionLocal() as db:
        from app.models.scanner import ScanJob

        job = db.get(ScanJob, job_id)
        if job is None:
            return BotReply("Цю серію вже видалено.")
        parts = series_scan.stored_parts(job)
        try:
            outcome = await series_scan.resolve(db, job, choice)
        except ValueError as exc:
            return BotReply(f"Не вдалося обробити серію: {exc}")
    return await _describe_batch(outcome, parts)


async def _describe_series(result, sent: int) -> "BotReply":
    """One answer for a glued receipt: how it was assembled and what came out."""
    from app.models.models import Receipt
    from app.models.scanner import ScanJob

    with SessionLocal() as db:
        scan_job = db.get(ScanJob, result.scan_job_id)
        raw = (scan_job.raw_ocr or {}) if scan_job else {}
        next_ai = _next_ai_step(db, scan_job)
        source = "\n".join(filter(None, (_source_line(scan_job), _trace_line(scan_job))))
        if result.receipt_id:
            receipt = db.get(Receipt, result.receipt_id)
            return _saved_receipt_reply(receipt, result, raw.get("qr_state"),
                                        raw.get("fiscal_url"), next_ai=next_ai,
                                        source=source)

    draft = raw.get("draft") or {}
    if not draft:
        return BotReply(
            "Жодне з фото не вдалося розпізнати. "
            "Спробуйте зняти чек ще раз, з перекриттям між кадрами.",
            keyboard=_review_keyboard(result.scan_job_id, save=False, next_ai=next_ai),
        )
    seams = [n for n in (raw.get("notes") or []) if "спільних" in n or "розрив" in n
             or "загубилася" in n]
    return BotReply(
        f"🧩 Склеєно з {raw.get('parts', sent)} фото.\n"
        + ("\n".join(seams) + "\n\n" if seams else "\n")
        + (f"{source}\n" if source else "")
        + format_draft_message(draft)
        + "\n\nПеревірте позиції: якщо чогось бракує — додайте ще одне фото "
          "з перекриттям.",
        keyboard=_review_keyboard(result.scan_job_id, next_ai=next_ai),
    )


def _shopping_instruction(text: str) -> bool:
    """Whether the caption explicitly asks for a shopping-list operation."""
    value = (text or "").casefold()
    return any(phrase in value for phrase in (
        "список покупок", "список покупок", "добавь в список", "додай в список",
        "додати до списку", "додай до списку",
    ))


async def _try_image_shopping(db: Session, data: bytes, media_type: str,
                              instruction: str, *, required: bool = False) -> BotReply | None:
    from app.services import capture

    if not load_config(db).capture_enabled:
        return BotReply("Розпізнавання списку покупок вимкнено в налаштуваннях AI.") if required else None
    try:
        draft = await capture.parse_image_capture(db, data, media_type, instruction=instruction)
    except AiConfigError:
        if required:
            return BotReply("AI не налаштовано — не можу прочитати список покупок з фото.")
        return None
    except Exception as exc:  # noqa: BLE001 — a normal receipt fallback must survive
        logger.info("image shopping-list capture unavailable: %s", exc)
        if required:
            return BotReply("Не вдалося прочитати список покупок. Надішліть чіткіше фото.")
        return None
    if draft.intent != "shopping" or not draft.shopping:
        return (BotReply(
            "Не вдалося впевнено розпізнати список покупок. "
            "Надішліть фото ближче, без відблисків і тіней."
        ) if required else None)
    rows = capture.add_shopping_items(db, draft, added_method="telegram-photo", added_by="Telegram")
    shopping_reply = _shopping_capture_reply(db, rows)
    return BotReply("📷 Розпізнав список покупок з фото.\n\n" + shopping_reply.text,
                    keyboard=shopping_reply.keyboard)


async def _scan_bytes(data: bytes, media_type: str, filename: str, *, chat_id: int | None = None,
                      instruction: str = "", shopping_first: bool = False,
                      uploader: str | None = None) -> "str | BotReply":
    if shopping_first:
        with SessionLocal() as db:
            shopping_reply = await _try_image_shopping(
                db, data, media_type, instruction, required=True,
            )
        if shopping_reply is not None:
            return shopping_reply

    # Import lazily to avoid coupling app startup to the API module graph.
    from app.api.inbox import _handle_receipt

    from app.scanner.fiscal_qr import parse_fiscal_url

    with SessionLocal() as db:
        cfg = load_config(db)
        pending_url = cfg.pending_fiscal_urls.get(str(chat_id)) if chat_id is not None else None
        result = await _handle_receipt(
            db, data, filename, media_type,
            fiscal_override=parse_fiscal_url(pending_url) if pending_url else None,
            uploader=uploader,
        )
        from app.models.scanner import ScanJob
        scan_job = db.get(ScanJob, result.scan_job_id)
        raw = (scan_job.raw_ocr or {}) if scan_job else {}
        fiscal_url, qr_state = raw.get("fiscal_url"), raw.get("qr_state")
        if not result.receipt_id:
            from app.services.fiscal_queue import enqueue
            if fiscal_url:
                enqueue(db, fiscal_url, source="telegram", chat_id=chat_id, scan_job_id=result.scan_job_id)
        next_ai = _next_ai_step(db, scan_job)
        source = "\n".join(filter(None, (_source_line(scan_job), _trace_line(scan_job))))
        if result.receipt_id:
            if pending_url:
                cfg.pending_fiscal_urls.pop(str(chat_id), None)
                save_config(db, cfg)
            from app.models.models import Receipt
            receipt = db.get(Receipt, result.receipt_id)
            return _saved_receipt_reply(receipt, result, qr_state, fiscal_url,
                                        next_ai=next_ai, source=source)
    # A photo can be a handwritten shopping list rather than a receipt.  Try
    # that interpretation only after the receipt pipeline found no usable
    # result, and only when capture is enabled; QR/receipt recognition remains
    # authoritative and never gets replaced by a guess about a list.
    if not result.receipt_id and not result.needs_review:
        with SessionLocal() as db:
            shopping_reply = await _try_image_shopping(db, data, media_type, instruction)
            if shopping_reply is not None:
                return shopping_reply

    qr_line = _qr_status_line(qr_state, fiscal_url)
    retake = "📷 Надішліть чіткіше фото чека у відповідь на це повідомлення."
    if result.needs_review:
        return BotReply(
            f"🟡 Чек розпізнано частково · метод {result.method}.\n"
            + (f"{source}\n" if source else "")
            + f"{qr_line}\n\n"
            f"{format_draft_message(raw.get('draft') or {})}\n"
            f"{retake}",
            keyboard=_review_keyboard(result.scan_job_id, next_ai=next_ai),
        )
    return BotReply(
        "Не вдалося надійно розпізнати чек.\n"
        + (f"{source}\n" if source else "")
        + f"{qr_line}\n{retake}",
        keyboard=_review_keyboard(result.scan_job_id, save=False, next_ai=next_ai),
    )


def format_draft_message(draft: dict) -> str:
    """What the scan actually read, so the answer can be judged in the chat.

    "Розпізнано частково" on its own says nothing about whether to accept it —
    the user had to open the inbox to find out. The mismatch between the lines
    and the printed total is what made this a draft, so it is spelled out.
    """
    if not draft:
        return "Розпізнаних даних немає — прийняти нічого."

    lines: list[str] = []
    if draft.get("shop_name"):
        lines.append(f"🏪 {draft['shop_name']}")
    else:
        lines.append("🏪 магазин не розпізнано")
    purchased = draft.get("purchased_at")
    if purchased:
        try:
            local = as_utc(dt.datetime.fromisoformat(str(purchased))).astimezone(KYIV)
            lines.append(f"🕒 {local:%d.%m.%Y %H:%M}")
        except ValueError:
            pass

    items = list(draft.get("items") or [])
    lines.append("")
    shown = 0
    for index, item in enumerate(items, 1):
        quantity = float(item.get("quantity") or 1)
        price = item.get("unit_price")
        detail = f"{quantity:g}"
        if price is not None:
            detail += f" × {_amount(price)}"
        detail += f" = {_amount(item.get('total_price'))}"
        block = f"{index}. {item.get('name') or '—'}\n   {detail}"
        # Leave room for the totals and the advice below Telegram's 4096 cap.
        if sum(len(line) + 1 for line in lines) + len(block) > 2800:
            break
        lines.append(block)
        shown += 1
    if not items:
        lines.append("Позицій не розпізнано.")
    elif shown < len(items):
        lines.append(f"…ще {len(items) - shown} позицій")

    lines.append("")
    items_sum = round(sum(float(i.get("total_price") or 0) for i in items), 2)
    total = draft.get("total_amount")
    lines.append(f"Сума позицій: {_amount(items_sum)}")
    if total is not None:
        lines.append(f"Разом на чеку: {_amount(total)}")
        delta = round(items_sum - float(total), 2)
        if delta:
            lines.append(f"⚠️ Розбіжність {delta:+.2f} — саме тому чек не збережено автоматично.")
        else:
            lines.append("✅ Позиції сходяться із сумою.")
    return "\n".join(lines)


def _saved_receipt_reply(receipt, result, qr_state, fiscal_url, *,
                         next_ai: tuple[str, str, str] | None = None,
                         source: str = "") -> "BotReply":
    """A saved receipt, with the rescan actions still attached.

    Even a receipt that filed cleanly can be a misread, so the rerun buttons
    stay available; the QR outcome is said out loud too — a decoded ДПС link
    tells the user the data is fiscal, and an unread QR tells them that
    reshooting it upgrades the guess to the ДПС document itself.
    """
    from app.scanner.fiscal_qr import QR_UNREADABLE

    text = format_receipt_message(receipt, duplicate=result.duplicate, method=result.method)
    if source:
        text += f"\n{source}"
    if fiscal_url or qr_state == QR_UNREADABLE:
        text += f"\n\n{_qr_status_line(qr_state, fiscal_url)}"
    return BotReply(text, keyboard=_review_keyboard(result.scan_job_id, saved=True,
                                                    next_ai=next_ai))


def _next_ai_step(db: Session, job) -> tuple[str, str, str] | None:
    """The route step to offer after the one this scan used, if any.

    Only offered once a model has actually read the photo: before that the
    ordinary "🤖 Розпізнати AI" button already starts the route at step 1, and
    two buttons doing the same thing is worse than one.
    """
    from app.services import ai, scan_jobs

    if job is None:
        return None
    used = scan_jobs.used_ai_step(job)
    if used is None:
        return None
    try:
        return ai.next_ocr_step(db, used)
    except Exception:  # noqa: BLE001 — a broken AI config must not break the reply
        logger.warning("could not read the AI route for scan %s", job.id, exc_info=True)
        return None


def _trace_line(job) -> str:
    """One line of "what it cost": the stages that actually spent time.

    The same record the inbox shows expanded — in a chat it has to fit on one
    line, so only the stages that took measurable time or tokens are named.
    """
    steps = (job.raw_ocr or {}).get("trace") or [] if job is not None else []
    parts: list[str] = []
    tokens = 0
    for step in steps:
        tokens += int(step.get("tokens_in") or 0) + int(step.get("tokens_out") or 0)
        ms = int(step.get("ms") or 0)
        if ms >= 100:
            parts.append(f"{step.get('stage')} {ms / 1000:.1f}с")
    if tokens:
        parts.append(f"{tokens} ток.")
    return "⏱ " + " · ".join(parts) if parts else ""


def _source_line(job) -> str:
    """"Розпізнано: QR" / "Розпізнано AI: <service> / <model>", or nothing."""
    from app.services import scan_jobs

    source = scan_jobs.recognised_by(job) if job is not None else ""
    return f"🔎 Розпізнано: {source}" if source else ""


def _qr_status_line(qr_state: str | None, fiscal_url: str | None) -> str:
    """One line telling the user what happened to the QR code on their photo."""
    from app.scanner.fiscal_qr import QR_DECODED, QR_UNREADABLE

    if fiscal_url:
        return ("✅ QR‑код розпізнано — це посилання на фіскальний чек ДПС.\n"
                f"🧾 {fiscal_url}")
    if qr_state == QR_DECODED:
        return "🔶 QR‑код прочитано, але це не фіскальне посилання ДПС — дані взято з тексту чека."
    if qr_state == QR_UNREADABLE:
        return ("🔶 QR‑код видно, але він замалий/розмитий, щоб його прочитати.\n"
                "Сфотографуйте окремо саме QR — зблизька, щоб він займав більшу частину кадру.")
    return "❌ QR‑код на фото не знайдено. Перевірте, чи він повністю потрапив у кадр."


def _persist_normalized(db: Session, receipt, *, method: str) -> str:
    from app.api.inbox import _persist_receipt, _scanner_account
    from app.models.models import Receipt
    from sqlalchemy import select

    account = _scanner_account(db)
    duplicate = db.execute(select(Receipt.id).where(
        Receipt.account_id == account.id, Receipt.external_id == receipt.external_id,
    )).scalar_one_or_none() is not None
    saved = _persist_receipt(db, account, receipt)
    duplicate = duplicate or saved.external_id != receipt.external_id
    return format_receipt_message(saved, duplicate=duplicate, method=method)


def _amount(value) -> str:
    return f"{float(value or 0):,.2f}".replace(",", " ")


def format_receipt_message(receipt, *, duplicate: bool = False, method: str | None = None) -> str:
    """Compact human-readable receipt followed by its app deep link."""
    title = "ℹ️ Чек вже є в базі" if duplicate else "✅ Чек додано"
    lines = [title]
    if receipt.shop_name:
        lines.append(f"🏪 {receipt.shop_name}")
    if receipt.purchased_at:
        local = as_utc(receipt.purchased_at).astimezone(KYIV)
        lines.append(f"🕒 {local:%d.%m.%Y %H:%M}")
    if receipt.shop_address:
        lines.append(f"📍 {receipt.shop_address}")
    if method:
        lines.append(f"Метод: {method}")

    lines.append("")
    items = list(receipt.items)
    # Keep enough room for totals and the link within Telegram's 4096-char cap.
    shown = 0
    for index, item in enumerate(items, 1):
        qty = float(item.quantity or 1)
        unit_price = float(item.unit_price) if item.unit_price is not None else None
        qty_text = f"{qty:g}{(' ' + item.unit) if item.unit else ''}"
        detail = f"{qty_text}"
        if unit_price is not None:
            detail += f" × {_amount(unit_price)}"
        detail += f" = {_amount(item.total_price)} грн"
        if float(item.discount or 0):
            detail += f" · знижка {_amount(item.discount)}"
        block = f"{index}. {item.name}\n   {detail}"
        if sum(len(line) + 1 for line in lines) + len(block) > 3300:
            break
        lines.append(block)
        shown += 1
    if shown < len(items):
        lines.append(f"…ще {len(items) - shown} позицій у застосунку")

    lines.append("")
    if float(receipt.total_discount or 0):
        lines.append(f"Знижка: {_amount(receipt.total_discount)} грн")
    lines.append(f"Разом: {_amount(receipt.total_amount)} {receipt.currency or 'UAH'}")
    if receipt.payment_type:
        payment = {"card": "картка", "cash": "готівка"}.get(receipt.payment_type, receipt.payment_type)
        lines.append(f"Оплата: {payment}")
    if receipt.receipt_url:
        lines.append(f"🧾 Оригінал: {receipt.receipt_url}")
    link = app_url(f"/#/receipts/{receipt.id}")
    if link:
        lines.append(f"🔗 У застосунку: {link}")
    return "\n".join(lines)


async def _save_review_draft(job_id: str) -> "str | BotReply":
    from app.models.scanner import ScanJob
    from app.services import scan_jobs

    with SessionLocal() as db:
        try:
            saved, duplicate = scan_jobs.save_draft(db, job_id)
        except scan_jobs.ScanJobError as exc:
            return str(exc)
        job = db.get(ScanJob, job_id)
        return BotReply(
            format_receipt_message(saved, duplicate=duplicate, method=job.method if job else None),
            keyboard=_review_keyboard(job_id, saved=True),
        )


def _delete_job(job_id: str, *, with_receipt: bool = False) -> str:
    from app.services import scan_jobs

    with SessionLocal() as db:
        try:
            receipt_id = scan_jobs.delete_job(db, job_id, with_receipt=with_receipt)
        except scan_jobs.ScanJobError as exc:
            return str(exc)
    return "🗑 Чек і сканування видалено." if receipt_id else "🗑 Видалено з інбоксу."


async def _rescan_job(job_id: str, *, ai_first: bool, replace: bool = False,
                      escalate: bool = False) -> "str | BotReply":
    from app.models.models import Receipt
    from app.models.scanner import ScanJob
    from app.services import scan_jobs

    from app.services import series_scan

    with SessionLocal() as db:
        try:
            job = db.get(ScanJob, job_id)
            if job is not None and series_scan.is_series(job) and not job.receipt_id:
                # A series lives in several frames; the ordinary rescan would
                # re-run only the first one and quietly shorten the receipt.
                result = await series_scan.rescan_series(db, job, ai_first=ai_first,
                                                         escalate=escalate)
            else:
                result = await scan_jobs.rescan_job(db, job_id, ai_first=ai_first,
                                                    replace=replace, escalate=escalate)
        except (scan_jobs.ScanJobError, ValueError) as exc:
            return str(exc)

        fresh = db.get(ScanJob, result.scan_job_id)
        raw = (fresh.raw_ocr or {})
        next_ai = _next_ai_step(db, fresh)
        source = "\n".join(filter(None, (_source_line(fresh), _trace_line(fresh))))
        if result.receipt_id:
            receipt = db.get(Receipt, result.receipt_id)
            reply = _saved_receipt_reply(receipt, result, raw.get("qr_state"),
                                         raw.get("fiscal_url"), next_ai=next_ai,
                                         source=source)
            if result.replaced_receipt_id:
                reply.text = "♻️ Попередній чек замінено.\n\n" + reply.text
            return reply

        head = (
            f"🟡 Знову лише частково · метод {result.method}."
            if result.needs_review
            else "Розпізнати не вдалося навіть так."
        )
        if result.kept_receipt_id:
            head += "\nРаніше збережений чек залишено без змін."
        body = f"\n{format_draft_message(raw.get('draft') or {})}" if result.needs_review else ""
        return BotReply(
            f"{head}\n" + (f"{source}\n" if source else "")
            + f"{_qr_status_line(raw.get('qr_state'), raw.get('fiscal_url'))}\n{body}\n"
            "📷 Або надішліть чіткіше фото у відповідь на це повідомлення.",
            keyboard=_review_keyboard(result.scan_job_id, save=result.needs_review),
        )


async def _fiscal_receipt(fiscal):
    from app.scanner import tax_cabinet
    from app.services import tax_cabinet_settings

    with SessionLocal() as db:
        return await tax_cabinet.fetch_structured(
            fiscal, api_token=tax_cabinet_settings.get_token(db),
        )


def _fiscal_unavailable_message(fiscal) -> str:
    details: list[str] = [
        "⚠️ ДПС не віддала структуровані дані через серверний API.",
        "У браузері цей чек відкривається через reCAPTCHA, яку бот не може проходити автоматично.",
        "",
    ]
    if fiscal.issued_at:
        local = as_utc(fiscal.issued_at).astimezone(KYIV)
        details.append(f"🕒 З QR: {local:%d.%m.%Y %H:%M:%S}")
    if fiscal.total_amount is not None:
        details.append(f"Разом з QR: {_amount(fiscal.total_amount)} UAH")
    details.extend([
        f"🧾 {fiscal.raw_url}",
        "",
        "Відкрийте посилання та надішліть боту повний скриншот чека ДПС, де видно шапку й усі товарні позиції. Бот збере реквізити QR та текст скриншота в один чек.",
    ])
    return "\n".join(details)


def _fiscal_url_from_html(page: str) -> str | None:
    """Return the first complete ДПС receipt URL embedded in a share page.

    Vchasno.Kasa renders the receipt server-side and includes both an og:image
    preview and a `cashregs/check?...` link. The latter must win: it contains
    fn/id/date/time/amount needed by the token-authenticated XML API, whereas
    OCR of the preview is intentionally only a fallback.
    """
    from app.scanner.fiscal_qr import parse_fiscal_url

    decoded = html.unescape(page)
    for candidate in _HTML_URL_RE.findall(decoded):
        candidate = candidate.rstrip(".,);]")
        if parse_fiscal_url(candidate) is not None:
            return candidate
    return None


async def _scan_url(url: str, *, chat_id: int | None = None) -> "str | BotReply":
    from app.scanner.fiscal_qr import parse_fiscal_url

    fiscal = parse_fiscal_url(url)
    if fiscal:
        receipt = await _fiscal_receipt(fiscal)
        if receipt is None:
            if chat_id is not None:
                with SessionLocal() as db:
                    from app.services.fiscal_queue import enqueue
                    enqueue(db, fiscal.raw_url, source="telegram", chat_id=chat_id)
                    cfg = load_config(db)
                    cfg.pending_fiscal_urls[str(chat_id)] = fiscal.raw_url
                    save_config(db, cfg)
            return _fiscal_unavailable_message(fiscal)
        with SessionLocal() as db:
            return _persist_normalized(db, receipt, method="tax_cabinet")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "Підтримуються лише HTTP/HTTPS-посилання."
    # Telegram text is untrusted input: never let a link turn the bot into an
    # SSRF proxy to localhost, the Docker network, or cloud metadata services.
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return "Посилання на локальну або службову адресу заборонено."
    # Public shared links (including Vchasno) usually resolve to an image/PDF.
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "ReceiptScanner/1.0"})
    response.raise_for_status()
    data = response.content
    if len(data) > _MAX_DOWNLOAD:
        return "Файл за посиланням більший за 40 МБ."
    media = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if media.startswith("image/"):
        return await _scan_bytes(data, media, parsed.path.rsplit("/", 1)[-1] or "link-image")
    # A share page may advertise a directly downloadable receipt image.
    if media == "text/html":
        page = data.decode(response.encoding or "utf-8", "ignore")
        fiscal_url = _fiscal_url_from_html(page)
        if fiscal_url:
            fiscal_receipt = await _fiscal_receipt(parse_fiscal_url(fiscal_url))
            if fiscal_receipt is not None:
                with SessionLocal() as db:
                    return _persist_normalized(db, fiscal_receipt, method="tax_cabinet")
            # The official API currently returns a wartime registry-access
            # restriction for some checks. Vchasno's server-rendered HTML is
            # still structured and much more accurate than OCR of og:image.
            from app.scanner.vchasno import parse_vchasno_html
            vchasno_receipt = parse_vchasno_html(page, source_url=url)
            if vchasno_receipt is not None:
                with SessionLocal() as db:
                    return _persist_normalized(db, vchasno_receipt, method="vchasno_html (ДПС недоступна)")
        match = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page, re.I)
        if match:
            return await _scan_url(html.unescape(match.group(1)), chat_id=chat_id)
    return "Посилання відкрито, але воно не віддає зображення чека. Надішліть фото або пряме посилання на файл."


def _receipt_day_bounds(value: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Return UTC-naive bounds for one Kyiv calendar day in SQLite."""
    start = dt.datetime.combine(value, dt.time.min, tzinfo=KYIV).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(value + dt.timedelta(days=1), dt.time.min,
                              tzinfo=KYIV).astimezone(dt.timezone.utc)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def _receipt_date_arg(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _receipts_reply(*, limit: int = 10, page: int = 0,
                    selected_date: dt.date | None = None) -> BotReply:
    """Render a paginated receipt index with one button per receipt."""
    from app.models.models import Receipt, Store

    limit = max(1, min(limit, 50))
    page = max(0, page)
    clauses = []
    date_label = "весь період"
    if selected_date:
        lo, hi = _receipt_day_bounds(selected_date)
        clauses.extend([Receipt.purchased_at >= lo, Receipt.purchased_at < hi])
        date_label = selected_date.strftime("%d.%m.%Y")

    with SessionLocal() as db:
        total = db.execute(select(func.count()).select_from(Receipt).where(*clauses)).scalar_one()
        pages = max(1, (total + limit - 1) // limit)
        page = min(page, pages - 1)
        rows = db.execute(
            select(Receipt).where(*clauses)
            .order_by(Receipt.purchased_at.desc())
            .offset(page * limit).limit(limit)
        ).scalars().all()
        store_names = {row.code: row.name for row in db.execute(select(Store)).scalars().all()}

        lines = [f"🧾 Чеки · {date_label}", f"Сторінка {page + 1}/{pages} · усього: {total}", ""]
        keyboard: list[list[dict]] = []
        for row in rows:
            local = as_utc(row.purchased_at).astimezone(KYIV) if row.purchased_at else None
            when = local.strftime("%d.%m %H:%M") if local else "без дати"
            shop = row.shop_name or store_names.get(row.store_code, row.store_code) or "Невідомий магазин"
            lines.append(f"{when} · {shop} · {_amount(row.total_amount)} {row.currency or 'UAH'}")
            keyboard.append([{
                "text": f"🔎 Деталі · {when} · {_amount(row.total_amount)}",
                "callback_data": f"rc:detail:{row.id}",
            }])

        if not rows:
            lines.append("Чеків не знайдено.")
        navigation = []
        if page > 0:
            date_key = selected_date.strftime("%Y%m%d") if selected_date else "all"
            navigation.append({
                "text": "◀️ Назад",
                "callback_data": f"rc:list:{page - 1}:{limit}:{date_key}",
            })
        if page + 1 < pages:
            date_key = selected_date.strftime("%Y%m%d") if selected_date else "all"
            navigation.append({
                "text": "Далі ▶️",
                "callback_data": f"rc:list:{page + 1}:{limit}:{date_key}",
            })
        if navigation:
            keyboard.append(navigation)
    return BotReply("\n".join(lines), keyboard=keyboard)


def _receipt_detail_reply(receipt_id: str) -> str:
    from app.models.models import Receipt

    with SessionLocal() as db:
        receipt = db.get(Receipt, receipt_id)
        if receipt is None:
            return "Чек уже недоступний."
        text = format_receipt_message(receipt)
    return text.replace("✅ Чек додано", "🧾 Деталі чека", 1)


def _parse_receipts_args(arg: str) -> str | BotReply:
    """Accept `/receipts`, `/receipts 20`, or `/receipts 20 2026-09-27`."""
    parts = arg.split()
    limit = 10
    selected_date = None
    if parts:
        if parts[0].isdigit():
            limit = int(parts.pop(0))
        if parts:
            selected_date = _receipt_date_arg(parts.pop(0))
        if parts or selected_date is None and arg and not arg.split()[0].isdigit():
            return "Формат: /receipts [кількість] [РРРР-ММ-ДД]"
    return _receipts_reply(limit=limit, selected_date=selected_date)


def _spending_chart(values: list[tuple[dt.date, float]], *, days: int, total: float,
                    receipt_count: int) -> bytes:
    """Build a small dependency-light bar chart suitable for Telegram."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1100, 620
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        regular = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        value_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except OSError:
        regular = title_font = value_font = ImageFont.load_default()

    draw.text((55, 32), f"Витрати за останні {days} днів", fill="#172033", font=title_font)
    draw.text((58, 82), f"Разом: {_amount(total)} грн · чеків: {receipt_count}",
              fill="#4b5563", font=regular)
    left, top, right, bottom = 90, 145, width - 55, height - 95
    max_value = max((value for _, value in values), default=0)
    max_value = max(max_value, 1)
    draw.line((left, bottom, right, bottom), fill="#cbd5e1", width=2)
    draw.line((left, top, left, bottom), fill="#cbd5e1", width=2)
    slot = (right - left) / max(len(values), 1)
    bar_width = max(8, int(slot * 0.62))
    label_step = max(1, len(values) // 14)
    for index, (day, value) in enumerate(values):
        x = left + slot * index + (slot - bar_width) / 2
        bar_top = bottom - (bottom - top) * value / max_value
        draw.rounded_rectangle((x, bar_top, x + bar_width, bottom), radius=7, fill="#2563eb")
        if value:
            label = _amount(value)
            bbox = draw.textbbox((0, 0), label, font=value_font)
            draw.text((x + (bar_width - bbox[2]) / 2, max(top, bar_top - 30)), label,
                      fill="#172033", font=value_font)
        if index % label_step == 0 or index == len(values) - 1:
            label = day.strftime("%d.%m")
            bbox = draw.textbbox((0, 0), label, font=regular)
            draw.text((x + (bar_width - bbox[2]) / 2, bottom + 18), label,
                      fill="#4b5563", font=regular)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _spending_reply(days: int = 7) -> BotReply | str:
    from app.api.dashboard import get_stats, timeseries

    days = max(1, min(days, 366))
    today = dt.datetime.now(KYIV).date()
    first_day = today - dt.timedelta(days=days - 1)
    start = dt.datetime.combine(first_day, dt.time.min, tzinfo=KYIV).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(today + dt.timedelta(days=1), dt.time.min,
                              tzinfo=KYIV).astimezone(dt.timezone.utc)
    with SessionLocal() as db:
        stats = get_stats(db=db, date_from=start, date_to=end)
        rows = timeseries(db=db, date_from=start, date_to=end, bucket="day")
    by_day = {dt.date.fromisoformat(row.period): float(row.total_spent) for row in rows}
    values = [(first_day + dt.timedelta(days=index),
               by_day.get(first_day + dt.timedelta(days=index), 0.0))
              for index in range(days)]
    caption = (f"📊 Витрати за останні {days} днів\n"
               f"💰 Разом: {_amount(stats.total_spent)} грн\n"
               f"🧾 Чеків: {stats.total_receipts}\n"
               f"Період: {first_day:%d.%m.%Y} — {today:%d.%m.%Y}")
    return BotReply(caption, photo=_spending_chart(
        values, days=days, total=float(stats.total_spent), receipt_count=stats.total_receipts,
    ))


def _mcp_command(text: str) -> str | BotReply | None:
    """Reuse the same functions exposed by MCP, without an internal HTTP hop."""
    from app.mcp_server import add_to_shopping_list, search_products

    command, _, arg = text.partition(" ")
    arg = arg.strip()
    if command == "/find":
        if not arg:
            return "Формат: /find молоко"
        rows = search_products(arg)[:8]
        return "\n".join(f"• {r.get('name')} — {r.get('last_price')} грн" for r in rows) or "Нічого не знайдено"
    if command == "/spending":
        if arg and not arg.isdigit():
            return "Формат: /spending [кількість днів]"
        return _spending_reply(int(arg) if arg else 7)
    if command == "/receipts":
        return _parse_receipts_args(arg)
    if command == "/add":
        if not arg:
            return "Формат: /add молоко"
        value = add_to_shopping_list(arg)
        return f"✅ Додано до списку: {value.get('raw_text') or arg}"
    if command in {"/shopping", "/list"}:
        with SessionLocal() as db:
            from app.services import shopping_list as shopping_svc
            from app.models.models import Category, Store
            rows = shopping_svc.active_items(db)
        pending = [r for r in rows if r.status.value == "pending"]
        bought = [r for r in rows if r.status.value == "bought"]
        lines = [f"🛒 Список покупок · до покупки: {len(pending)}"]
        for row in pending[:40]:
            qty = (f" × {float(row.qty):g}{(' ' + row.unit) if row.unit else ''}"
                   if row.qty and float(row.qty) != 1 else "")
            meta = []
            if row.target_store_code:
                store = db.execute(select(Store).where(Store.code == row.target_store_code)).scalar_one_or_none()
                meta.append(f"🏪 {store.name if store else row.target_store_code}")
            if row.category_id:
                meta.append(f"📂 {shopping_svc.category_name_ru(db.get(Category, row.category_id))}")
            if row.tag:
                meta.append(f"🏷 {row.tag}")
            note = f" ({row.note})" if row.note else ""
            lines.append(f"☐ {row.raw_text}{note}{qty}" + (f" · {' · '.join(meta)}" if meta else ""))
        if len(pending) > 40:
            lines.append(f"…ще {len(pending) - 40}")
        if bought:
            lines.append(f"\n✅ Вже куплено: {len(bought)}")
        return "\n".join(lines) if rows else "🛒 Список покупок порожній."
    if command in {"/buy", "/куплено"}:
        if not arg:
            return "Формат: /buy молоко"
        with SessionLocal() as db:
            from app.services import shopping_list as shopping_svc
            trip_item = shopping_svc.mark_trip_by_text(db, arg)
            row = db.get(shopping_svc.ShoppingListItem, trip_item.shopping_list_item_id) if trip_item else None
        return (f"✅ Позначено як куплене: {row.raw_text}"
                if row else "Не знайшов точного товару в активному чеклисте.")
    if command == "/trip":
        with SessionLocal() as db:
            from app.services import shopping_list as shopping_svc
            trip = shopping_svc.create_trip(db)
            rows = db.query(shopping_svc.ShoppingTripItem).filter(
                shopping_svc.ShoppingTripItem.trip_id == trip.id
            ).all()
        lines = [f"🛒 Чеклист готовий · позицій: {len(rows)}"]
        lines.extend(
            f"☐ {row.raw_text}{f' ({row.note})' if row.note else ''}"
            for row in rows
        )
        lines.append("\nПозначити: /buy товар · завершити: /trip_done")
        return "\n".join(lines)
    if command == "/trip_done":
        with SessionLocal() as db:
            from app.models.shopping import ShoppingTrip, ShoppingTripStatus
            from app.services import shopping_list as shopping_svc
            trip = db.query(ShoppingTrip).filter(ShoppingTrip.status == ShoppingTripStatus.active).first()
            if trip is None:
                return "Активного чеклиста немає. Почніть через /trip."
            shopping_svc.complete_trip(db, trip)
        return "✅ Чеклист завершено. Невідмічені товари залишилися у списку."
    if command == "/suggestions":
        return _suggestions_reply()
    return None


def _trip_reply(trip_id: str | None = None, store_code: str | None = None) -> BotReply:
    """Render the active shopping trip as an inline, Telegram-native-like list."""
    from app.models.models import Store
    from app.models.shopping import ShoppingTrip, ShoppingTripItem, ShoppingTripItemStatus, ShoppingTripStatus
    from app.services import shopping_list as shopping_svc

    with SessionLocal() as db:
        trip = db.get(ShoppingTrip, trip_id) if trip_id else db.query(ShoppingTrip).filter(
            ShoppingTrip.status == ShoppingTripStatus.active
        ).first()
        if trip is None and trip_id is None:
            trip = shopping_svc.create_trip(db, store_code=store_code)
        elif trip is not None and trip_id is None:
            trip = shopping_svc.create_trip(db, store_code=store_code)
        if trip is None:
            return BotReply("Активного чеклиста немає. Створіть його командою /trip.")
        rows = db.query(ShoppingTripItem).filter(ShoppingTripItem.trip_id == trip.id).order_by(
            ShoppingTripItem.created_at.asc()
        ).all()
        store = db.query(Store).filter(Store.code == trip.store_code).first() if trip.store_code else None
        done = sum(row.status == ShoppingTripItemStatus.bought for row in rows)
        keyboard = []
        for row in rows:
            mark = "☑" if row.status == ShoppingTripItemStatus.bought else "☐"
            note = f" ({row.note})" if row.note else ""
            keyboard.append([{"text": f"{mark} {row.raw_text}{note}",
                              # The Bot API caps callback_data at 64 bytes;
                              # the item id is globally unique, so trip id is
                              # resolved from the row after the click.
                              "callback_data": f"tri:item:{row.id}"}])
        keyboard.append([{"text": "✅ Завершить поход", "callback_data": f"tri:done:{trip.id}"}])
        store_name = store.name if store else (trip.store_code or "Все магазины")
        return BotReply(
                        f"🛒 Чеклист · магазин: {store_name}\n"
                        f"Позиции: {len(rows)} · выполнено: {done}",
                        keyboard=keyboard)


def _uncategorized_trip_menu(rows: list) -> BotReply:
    """Offer an explicit choice before starting a store checklist."""
    return BotReply(
        f"🗂 Є товарів без категорії: {len(rows)}. Що зробити перед покупками?",
        keyboard=[
            [{"text": "🧠 Розподілити", "callback_data": "tri:categorize"}],
            [{"text": "👀 Показати", "callback_data": "tri:uncategorized"}],
            [{"text": "🛒 Перейти до покупок", "callback_data": "tri:continue"}],
        ],
    )


def _uncategorized_trip_reply() -> BotReply:
    from app.services import shopping_list as shopping_svc

    with SessionLocal() as db:
        rows = shopping_svc.uncategorized_pending(db)
    if not rows:
        return BotReply("✅ Усі товари вже мають категорію.", keyboard=[
            [{"text": "🛒 Перейти до покупок", "callback_data": "tri:continue"}],
        ])
    lines = [f"🗂 Без категорії ({len(rows)}):"]
    for row in rows[:40]:
        qty = (f" — {float(row.qty):g}{(' ' + row.unit) if row.unit else ''}"
               if row.qty and float(row.qty) != 1 else "")
        lines.append(f"☐ {row.raw_text}{qty}")
    if len(rows) > 40:
        lines.append(f"…ще {len(rows) - 40}")
    return BotReply("\n".join(lines), keyboard=[
        [{"text": "🧠 Розподілити", "callback_data": "tri:categorize"}],
        [{"text": "🛒 Перейти до покупок", "callback_data": "tri:continue"}],
    ])


def _trip_store_picker(*, check_uncategorized: bool = True,
                       intro: str = "") -> BotReply:
    from app.models.models import Store
    from app.services import shopping_list as shopping_svc
    with SessionLocal() as db:
        if check_uncategorized:
            uncategorized = shopping_svc.uncategorized_pending(db)
            if uncategorized:
                return _uncategorized_trip_menu(uncategorized)
        stores = [(store, shopping_svc.store_pending_item_count(db, store.code))
                  for store in db.query(Store).filter(Store.network_type.is_not(None))
                  .order_by(Store.name.asc()).all()]
        stores = [(store, count) for store, count in stores if count > 0]
    if not stores:
        reply = _trip_reply()
        if intro:
            reply.text = intro + "\n\n" + reply.text
        return reply
    text = "🛒 В какой магазин идём? Выберите магазин или нажмите «Все магазины»."
    if intro:
        text = intro + "\n\n" + text
    return BotReply(
        text,
        keyboard=[
            [{"text": f"{store.name} · {count}", "callback_data": f"tri:store:{store.code}"}
             for store, count in stores[i:i + 2]]
            for i in range(0, len(stores), 2)
        ] + [[{"text": "Все магазины", "callback_data": "tri:store:"}]],
    )


def _suggestions_reply() -> BotReply:
    with SessionLocal() as db:
        from app.services import shopping_list as shopping_svc
        rows = shopping_svc.routine_suggestions(db)
    if not rows:
        return BotReply("🔔 Прострочених регулярних покупок немає.")
    lines = ["🔔 Можливо, вже час купити:"]
    keyboard = []
    for row in rows[:20]:
        lines.append(f"• {row['display_name']} — раз на {row['interval_days']:g} дн., прострочено {row['days_overdue']} дн.")
        token = shopping_svc.routine_token(row["normalized_name"])
        keyboard.append([
            {"text": f"➕ {row['display_name']}", "callback_data": f"rec:add:{token}"},
            {"text": "⏰ Пізніше", "callback_data": f"rec:later:{token}"},
            {"text": "🚫 Не нагадувати", "callback_data": f"rec:ignore:{token}"},
        ])
        if row.get("category_id"):
            keyboard.append([{"text": f"🚫 Ігнорувати категорію {row.get('category_path') or ''}"[:60],
                              "callback_data": f"rec:cat:{row['category_id']}"}])
    return BotReply("\n".join(lines), keyboard=keyboard)


async def _voice_reply(token: str, media: dict, *, message_id: int | None = None,
                       chat_id: int | None = None) -> "BotReply":
    """A dictated message: keep the audio, then try to read it.

    The recording is stored before anything else — recognition may be switched
    off, unconfigured or wrong, and a voice note is usually said once. When it
    cannot be read the answer says why and offers to try again; the audio itself
    stays available in the inbox either way.
    """
    from app.services import voice as voice_svc

    file_id = media.get("file_id")
    if not file_id:
        return BotReply("Не вдалося отримати голосове повідомлення.")
    data, media_type = await _download_telegram_file(token, file_id)
    media_type = media.get("mime_type") or media_type or "audio/ogg"

    with SessionLocal() as db:
        row = voice_svc.store(
            db, data, content_type=media_type, duration_s=media.get("duration"),
            source="telegram", chat_id=chat_id, message_id=message_id,
            filename=media.get("file_name"),
        )
        voice_id = row.id
        try:
            text = await voice_svc.transcribe(db, row)
        except Exception as exc:  # noqa: BLE001 — SttError or a missing file
            return BotReply(
                f"🎙 Голосове збережено{_voice_length(media)}, але не розпізнано: {exc}",
                keyboard=_voice_keyboard(voice_id, transcript=False, language=row.language),
            )
        used = row.language

    heard = f"🗣 «{text}»"
    reply = await _capture_reply(text)
    if reply is None:
        with SessionLocal() as db:
            voice_svc.note_applied(db, voice_svc.get(db, voice_id),
                                   "не схоже ні на чек, ні на список покупок")
        return BotReply(
            f"{heard}\n\nНе зрозумів, що з цим зробити. Скажіть «додай чек …», "
            "«додай в список покупок …» або «заправився … на … грн».",
            keyboard=_voice_keyboard(voice_id, transcript=True, language=used),
        )
    _note_voice_applied(voice_id, reply.text)
    # The phrase was understood — but if it was heard in the wrong language the
    # wording is nonsense, so the retry buttons stay under the answer.
    return BotReply(f"{heard}\n\n{reply.text}",
                    keyboard=(reply.keyboard or []) + _language_rows(voice_id, used))


def _note_voice_applied(voice_id: str, reply_text: str) -> None:
    """Keep the action summary, including shopping-list item details."""
    from app.services import voice as voice_svc

    lines = [line.strip() for line in reply_text.splitlines() if line.strip()]
    applied = lines[0] if lines else ""
    for index, line in enumerate(lines):
        if line.startswith("🛒 Додано до списку"):
            applied = " ".join(lines[index:])
            break

    with SessionLocal() as db:
        row = voice_svc.get(db, voice_id)
        if row is not None:
            voice_svc.note_applied(db, row, applied[:500])


def _voice_length(media: dict) -> str:
    duration = media.get("duration")
    return f" ({int(duration)} с)" if duration else ""


def _language_rows(voice_id: str, language: str | None) -> list[list[dict]]:
    """One button per configured dictation language except the one just used."""
    from app.services import ai, stt

    with SessionLocal() as db:
        # With recognition off the buttons could only fail — the message already
        # says so, and "🎧 Розпізнати ще раз" is the one retry worth keeping.
        if not stt.stt_ready(db):
            return []
        others = [c for c in stt.language_options(db) if c != (language or "")]
    return [
        [{"text": f"🌐 Розпізнати: {ai.speech_language_label(code)}",
          "callback_data": f"voi:lang:{code}:{voice_id}"}]
        for code in others[:3]
    ]


def _voice_keyboard(voice_id: str, *, transcript: bool, language: str | None = None) -> list[list[dict]]:
    """Retry actions under a voice message.

    Re-recognising costs a provider call, so when a transcript already exists the
    cheaper "read the same text again" comes first — that is what helps after
    fixing the AI settings. Below it, one button per configured language other
    than the one just used: a Ukrainian recogniser mangles a Russian sentence and
    the fix is to say "recognise this same recording as Russian", not to re-dictate.
    """
    rows: list[list[dict]] = []
    if transcript:
        rows.append([{"text": "🔁 Розібрати ще раз", "callback_data": f"voi:apply:{voice_id}"}])
    rows.append([{"text": "🎧 Розпізнати ще раз", "callback_data": f"voi:stt:{voice_id}"}])
    rows += _language_rows(voice_id, language)
    inbox_url = app_url("/#/inbox")
    if inbox_url:
        rows.append([{"text": "▶️ Прослухати в Інбоксі", "url": inbox_url}])
    return rows


async def _retry_voice(voice_id: str, *, recognise: bool,
                       language: str | None = None) -> "BotReply":
    """Run a stored voice message through recognition and/or reading again.

    `language` forces one of the configured dictation languages — the "🌐" button
    under a transcript that came out in the wrong one.
    """
    from app.services import ai, voice as voice_svc

    with SessionLocal() as db:
        row = voice_svc.get(db, voice_id)
        if row is None:
            return BotReply("Це голосове повідомлення вже видалено.")
        text = row.transcript
        if recognise or language or not text:
            try:
                text = await voice_svc.transcribe(db, row, language=language)
            except Exception as exc:  # noqa: BLE001 — SttError or a missing file
                return BotReply(f"🎙 Не вдалося розпізнати: {exc}",
                                keyboard=_voice_keyboard(voice_id, transcript=bool(row.transcript),
                                                         language=row.language))
        used = row.language

    heard = f"🗣 «{text}»"
    if language:
        heard += f" ({ai.speech_language_label(language)})"
    reply = await _capture_reply(text)
    if reply is None:
        return BotReply(f"{heard}\n\nНе зрозумів, що з цим зробити.",
                        keyboard=_voice_keyboard(voice_id, transcript=True, language=used))
    _note_voice_applied(voice_id, reply.text)
    return BotReply(f"{heard}\n\n{reply.text}",
                    keyboard=(reply.keyboard or []) + _language_rows(voice_id, used))


async def _capture_reply(text: str) -> "BotReply | None":
    """A plain sentence → a receipt draft or shopping-list entries.

    Returns None when the message wasn't an instruction at all, so the caller
    keeps its usual answer. Nothing is booked as a receipt here: the reading is
    filed as a draft and confirmed with the same buttons a photo gets, because a
    misheard price has to be visible before it lands in the ledger.
    """
    from app.services import capture
    from app.services import shopping_list as shopping_svc

    with SessionLocal() as db:
        if not load_config(db).capture_enabled:
            return None
        try:
            draft = await capture.parse_capture(db, text)
        except AiConfigError as exc:
            logger.info("capture unavailable: %s", exc)
            return BotReply("AI не налаштовано — розбір вільного тексту недоступний. "
                            "Увімкніть модель у Налаштуваннях → AI.")

        if draft.intent == "receipt":
            job = capture.create_receipt_job(db, draft, method="text")
            return BotReply(
                "📝 Ось що я зрозумів. Перевірте й підтвердьте:\n\n"
                + format_draft_message(draft.draft_dict()),
                keyboard=_review_keyboard(job.id, rescan=False),
            )

        if draft.intent == "fuel":
            try:
                res = capture.record_fuel(db, draft)
            except ValueError as exc:
                # Unknown station or no saved price — say which, the user can
                # refresh prices on the dashboard and repeat the message.
                return BotReply(f"⛽ Не вдалося записати заправку: {exc}")
            return BotReply(_fuel_message(res), keyboard=[
                [{"text": "↩️ Скасувати", "callback_data": f"cap:undor:{res['id']}"}],
            ])

        if draft.intent == "shopping":
            rows = capture.add_shopping_items(db, draft, added_method="telegram", added_by="Telegram")
            return _shopping_capture_reply(db, rows)
            batch = _remember_capture_batch(db, [row.id for row in rows])
            from app.models.models import Category, Store
            lines = [f"🛒 Додано до списку ({len(rows)}):"]
            common_store = rows[0].target_store_code if rows and all(
                row.target_store_code and row.target_store_code == rows[0].target_store_code for row in rows
            ) else None
            common_category = rows[0].category_id if rows and all(
                row.category_id and row.category_id == rows[0].category_id for row in rows
            ) else None
            common_tag = rows[0].tag if rows and all(
                row.tag and row.tag == rows[0].tag for row in rows
            ) else None
            if common_store:
                store = db.execute(select(Store).where(Store.code == common_store)).scalar_one_or_none()
                lines.append(f"🏪 магазин: {store.name if store else common_store}")
            if common_category:
                lines.append(f"📂 категория: {shopping_svc.category_name_ru(db.get(Category, common_category))}")
            if common_tag:
                lines.append(f"🏷 тег: {common_tag}")
            states = {getattr(row, "_shopping_add_state", "new") for row in rows}
            if states - {"new"}:
                lines.append("🆕 новая · ✏️ изменена · ↩️ уже была")
            for row in rows:
                qty = f" — {float(row.qty):g}{(' ' + row.unit) if row.unit else ''}" if row.qty and float(row.qty) != 1 else ""
                note = f" ({row.note})" if row.note else ""
                state = {
                    "new": "🆕",
                    "changed": "✏️",
                    "existing": "↩️",
                }.get(getattr(row, "_shopping_add_state", "new"), "🆕")
                lines.append(f"{state} {row.raw_text}{qty}{note}")
            return BotReply("\n".join(lines), keyboard=[
                [{"text": "↩️ Скасувати", "callback_data": f"cap:undo:{batch}"}],
            ])

    return None


def _fuel_message(res: dict) -> str:
    """Spell out the whole calculation: only the sum (or only the litres) was
    said, the rest came from the station's price list and has to be checkable."""
    return "\n".join([
        f"⛽ Заправку записано: {res['store_name']} · {res['account_label']}",
        f"{res['fuel']} — {float(res['liters']):g} л × {_amount(res['price_per_liter'])} "
        f"= {_amount(res['total_amount'])} грн",
        f"({res['price_source']})",
    ])


def _undo_fuel_receipt(receipt_id: str) -> str:
    """Delete a just-recorded fuel receipt."""
    from app.models.models import Receipt
    from app.services.scan_jobs import _delete_receipt

    with SessionLocal() as db:
        if db.get(Receipt, receipt_id) is None:
            return "Цей чек уже видалено."
        _delete_receipt(db, receipt_id)
    return "↩️ Заправку прибрано."


def _remember_capture_batch(db: Session, item_ids: list[str]) -> str:
    """Keep one captured batch undoable, dropping the oldest ones."""
    batch = uuid.uuid4().hex[:12]
    cfg = load_config(db)
    batches = dict(cfg.capture_batches)
    batches[batch] = item_ids
    for stale in list(batches)[:-_CAPTURE_BATCH_MEMORY]:
        batches.pop(stale, None)
    cfg.capture_batches = batches
    save_config(db, cfg)
    return batch


def _undo_capture_batch(batch: str) -> str:
    """Take back the shopping-list entries one captured message added."""
    from app.models.shopping import ShoppingListItem

    with SessionLocal() as db:
        cfg = load_config(db)
        item_ids = cfg.capture_batches.pop(batch, None)
        if item_ids is None:
            return "Цю дію вже неможливо скасувати."
        removed = 0
        for item_id in item_ids:
            row = db.get(ShoppingListItem, item_id)
            if row is not None:
                db.delete(row)
                removed += 1
        db.commit()
        save_config(db, cfg)
    return f"↩️ Прибрано зі списку: {removed}." if removed else "Ці позиції вже видалено."


def _sender_label(message: dict) -> str | None:
    """Short, readable Telegram identity for the scan journal."""
    sender = message.get("from") or {}
    name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
    username = str(sender.get("username") or "").strip().lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else None)


async def handle_update(token: str, update: dict) -> None:
    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not isinstance(chat_id, int):
        return
    text = (message.get("text") or message.get("caption") or "").strip()
    reply_to = message.get("message_id") if isinstance(message.get("message_id"), int) else None
    thread_id = message.get("message_thread_id") if isinstance(message.get("message_thread_id"), int) else None
    log_id: str | None = None
    with SessionLocal() as db:
        authorized = _authorized(db, chat_id)
        log_row = record_incoming_message(db, update, authorized=authorized)
        log_id = log_row.id if log_row else None
        if not authorized:
            is_new = request_approval(db, message)
            finish_incoming_message(log_id, status="rejected", outcome="authorization required")
            await _send(
                token, chat_id,
                "⏳ Запит на доступ надіслано власнику. Очікуйте підтвердження."
                if is_new else "⏳ Запит на доступ ще очікує підтвердження.",
                reply_to=reply_to, thread_id=thread_id,
            )
            return
        update_authorized_identity(db, message)

    try:
        if text in {"/start", "/help"}:
            answer = ("Надішліть фото/QR чека або посилання.\n"
                      "Довгий чек знімайте кількома кадрами з перекриттям і "
                      "надсилайте одним альбомом — вони склеяться в один чек "
                      "(або кнопка «➕ Додати ще фото» під чернеткою).\n\n"
                      "Можна написати або надиктувати звичайною мовою:\n"
                      "• «додай чек перукарня Вікторія на Індустріальній, "
                      "стрижка - 600, покраска - 1200»\n"
                      "• «додай в список покупок яйця 3 десятка, морква, молоко»\n"
                      "• «був на барс і заправився газом на 1000грн» — літри "
                      "порахуються за ціною станції\n\n"
                      "Основні розділи відкриваються через меню: чеклист покупок, "
                      "історія чеків і витрати. Товар можна просто "
                      "написати звичайною мовою — бот сам додасть або знайде його.")
        elif text == "/cancel":
            with SessionLocal() as db:
                cfg = load_config(db)
                cfg.pending_fiscal_urls.pop(str(chat_id), None)
                save_config(db, cfg)
            answer = "Очікування скриншота скасовано."
        elif message.get("voice") or message.get("audio") or message.get("video_note"):
            await _ack(token, chat_id, reply_to, thread_id, "🎧 Слухаю…")
            answer = await _voice_reply(
                token, message.get("voice") or message.get("audio") or message["video_note"],
                message_id=message.get("message_id"), chat_id=chat_id)
        elif message.get("photo"):
            media_group_id = message.get("media_group_id")
            pending_job = _take_pending_series(chat_id)
            if media_group_id:
                # Several photos sent at once are one thing to answer about. The
                # frames are gathered off the polling loop — waiting here would
                # block the very updates being waited for.
                first = _album_collect(str(media_group_id), message["photo"][-1]["file_id"])
                if first:
                    await _ack(token, chat_id, reply_to, thread_id,
                               "📥 Отримую фото, зачекайте…")
                    asyncio.create_task(_album_worker(
                        token, str(media_group_id),
                        chat_id=chat_id, reply_to=reply_to, thread_id=thread_id))
                finish_incoming_message(log_id, status="handled",
                                        outcome="фото альбому")
                return
            else:
                photo = message["photo"][-1]
                await _ack(token, chat_id, reply_to, thread_id)
                data, media = await _download_telegram_file(token, photo["file_id"])
                if pending_job:
                    answer = await _scan_batch_parts(
                        [(data, media, "Telegram · наступна частина")],
                        chat_id=chat_id, job_id=pending_job)
                else:
                    answer = await _scan_bytes(data, media, "Telegram · фото чека",
                                               chat_id=chat_id, instruction=text,
                                               shopping_first=_shopping_instruction(text),
                                               uploader=_sender_label(message))
        elif message.get("document") and str(message["document"].get("mime_type", "")).startswith("image/"):
            doc = message["document"]
            await _ack(token, chat_id, reply_to, thread_id)
            data, media = await _download_telegram_file(token, doc["file_id"])
            answer = await _scan_bytes(data, media, doc.get("file_name") or "telegram-image",
                                       chat_id=chat_id, instruction=text,
                                       shopping_first=_shopping_instruction(text),
                                       uploader=_sender_label(message))
        elif (url_match := _URL_RE.search(text)):
            await _ack(token, chat_id, reply_to, thread_id)
            answer = await _scan_url(url_match.group(0).rstrip(".,)"), chat_id=chat_id)
        else:
            # Slash commands and links first; only a message that is none of
            # those is worth spending an AI call on.
            if text == "/trip":
                answer = _trip_store_picker()
            elif text.startswith("/trip "):
                answer = _trip_reply(store_code=text.partition(" ")[2].strip() or None)
            elif text == "/suggestions":
                answer = _suggestions_reply()
            else:
                answer = _mcp_command(text)
            if answer is None and text:
                await _ack(token, chat_id, reply_to, thread_id, "🧠 Читаю повідомлення…")
                answer = await _capture_reply(text)
            if answer is None:
                answer = ("Надішліть фото, QR або посилання на чек — або просто напишіть "
                          "«додай чек ...» / «додай в список покупок ...». Відкрити "
                          "основні розділи можна через меню команд.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram update failed")
        answer = f"Не вдалося обробити: {exc}"
        finish_incoming_message(log_id, status="error", error=str(exc))
    reply = answer if isinstance(answer, BotReply) else BotReply(str(answer))
    if not str(answer).startswith("Не вдалося обробити:"):
        finish_incoming_message(log_id, status="processed", outcome=reply.text)
    if reply.photo:
        await _send_photo(token, chat_id, reply.photo, html.escape(reply.text),
                          reply_to=reply_to, thread_id=thread_id, keyboard=reply.keyboard)
    else:
        await _send(token, chat_id, html.escape(reply.text), reply_to=reply_to,
                    thread_id=thread_id, keyboard=reply.keyboard)


async def handle_callback(token: str, update: dict) -> None:
    query = update.get("callback_query") or {}
    data = str(query.get("data") or "")
    query_id = query.get("id")
    source = query.get("message") or {}
    chat_id = (source.get("chat") or {}).get("id")
    prompt_id = source.get("message_id") if isinstance(source.get("message_id"), int) else None
    thread_id = source.get("message_thread_id") if isinstance(source.get("message_thread_id"), int) else None
    if not isinstance(chat_id, int) or not query_id:
        return
    with SessionLocal() as db:
        authorized = _authorized(db, chat_id)
    if not authorized:
        await _api(token, "answerCallbackQuery", {"callback_query_id": query_id, "text": "Немає доступу"})
        return

    # "rev:<action>:<scan job id>"; an action ending in "x" replaces the
    # receipt this scan already produced.
    prefix, _, rest = data.partition(":")
    action, _, job_id = rest.partition(":")

    # Telegram spins the button until the callback is answered and gives up on
    # it after a few seconds — so answer first and do the work after. Waiting
    # until the end (recognition can take half a minute) is what made a press
    # look like nothing had happened.
    await _api(token, "answerCallbackQuery", {"callback_query_id": query_id})
    started = _started_line(prefix, action)
    if started:
        if prefix == "tri" and prompt_id is not None:
            try:
                await _edit_message(token, chat_id, prompt_id, started)
            except Exception:  # noqa: BLE001 — final result may still be shown
                logger.debug("could not show started shopping action", exc_info=True)
        else:
            await _ack(token, chat_id, prompt_id, thread_id, started)

    answer: "str | BotReply | None" = None
    try:
        if prefix == "rc" and action == "detail" and job_id:
            answer = _receipt_detail_reply(job_id)
        elif prefix == "rc" and action == "list" and job_id:
            page_text, _, limit_text = job_id.partition(":")
            limit_text, _, date_key = limit_text.partition(":")
            selected_date = None
            if date_key and date_key != "all":
                try:
                    selected_date = dt.datetime.strptime(date_key, "%Y%m%d").date()
                except ValueError:
                    answer = "Не вдалося прочитати дату сторінки."
            if answer is None:
                try:
                    answer = _receipts_reply(
                        page=int(page_text), limit=int(limit_text), selected_date=selected_date,
                    )
                except ValueError:
                    answer = "Сторінка чеків недоступна."
        elif prefix == "cap" and action == "undo" and job_id:
            answer = _undo_capture_batch(job_id)
        elif prefix == "tri" and action == "categorize":
            from app.services import shopping_list as shopping_svc
            with SessionLocal() as db:
                result = await shopping_svc.categorize_pending(db)
            summary = (
                f"✅ Розподілення завершено: {result['categorized']} з "
                f"{result['checked']} товарів отримали категорію."
            )
            if result.get("ai_error"):
                summary += "\n⚠️ Частину не вдалося розподілити через помилку AI."
            answer = _trip_store_picker(check_uncategorized=False, intro=summary)
        elif prefix == "tri" and action == "uncategorized":
            answer = _uncategorized_trip_reply()
        elif prefix == "tri" and action == "continue":
            answer = _trip_store_picker(check_uncategorized=False)
        elif prefix == "tri" and action == "store":
            # Empty job_id means the user explicitly chose "Все магазины";
            # preserve that distinction instead of turning it into no choice.
            answer = _trip_reply(store_code=job_id)
        elif prefix == "tri" and action == "item" and job_id:
            from app.models.shopping import ShoppingTrip, ShoppingTripItem, ShoppingTripItemStatus
            from app.services import shopping_list as shopping_svc
            with SessionLocal() as db:
                item = db.get(ShoppingTripItem, job_id)
                trip = db.get(ShoppingTrip, item.trip_id) if item else None
                if trip is None or item is None:
                    answer = "Цей чеклист вже недоступний."
                else:
                    status = (ShoppingTripItemStatus.planned
                              if item.status == ShoppingTripItemStatus.bought
                              else ShoppingTripItemStatus.bought)
                    shopping_svc.update_trip_item(db, item, status)
                    answer = _trip_reply(trip.id)
        elif prefix == "tri" and action == "done" and job_id:
            from app.models.shopping import ShoppingTrip, ShoppingTripStatus
            from app.services import shopping_list as shopping_svc
            with SessionLocal() as db:
                trip = db.get(ShoppingTrip, job_id)
                if trip is None or trip.status != ShoppingTripStatus.active:
                    answer = "Цей чеклист вже завершено."
                else:
                    shopping_svc.complete_trip(db, trip)
                    answer = "✅ Чеклист завершено. Невідмічені товари залишилися у списку."
        elif prefix == "rec" and action in {"add", "later", "ignore"} and job_id:
            from app.services import shopping_list as shopping_svc
            with SessionLocal() as db:
                name = shopping_svc.routine_name_by_token(db, job_id)
                if name is None:
                    answer = "Ця рекомендація вже неактуальна."
                else:
                    action_name = {"add": "add", "later": "postpone", "ignore": "ignore"}[action]
                    shopping_svc.routine_action(db, name, action_name)
                    answer = _suggestions_reply()
        elif prefix == "rec" and action == "cat" and job_id:
            from app.services import shopping_list as shopping_svc
            with SessionLocal() as db:
                shopping_svc.routine_category_action(db, job_id, "ignore")
            answer = _suggestions_reply()
        elif prefix == "cap" and action == "undor" and job_id:
            answer = _undo_fuel_receipt(job_id)
        elif prefix == "ser" and action in {"one", "many"} and job_id:
            answer = await _resolve_series_choice(
                job_id, "one" if action == "one" else "separate")
        elif prefix == "ser" and action == "add" and job_id:
            _arm_pending_series(chat_id, job_id)
            answer = ("📷 Надішліть наступне фото цього чека. Зніміть його так, щоб "
                      "кілька верхніх рядків повторювали нижні рядки попереднього "
                      "кадру — по них кадри і склеюються.")
        elif prefix == "voi" and job_id and action in {"stt", "apply"}:
            answer = await _retry_voice(job_id, recognise=action == "stt")
        elif prefix == "voi" and action == "lang" and job_id:
            # "voi:lang:<BCP-47>:<voice id>" — recognise the same recording again
            # in the language the button names.
            code, _, vid = job_id.partition(":")
            if vid:
                answer = await _retry_voice(vid, recognise=True, language=code)
        elif prefix == "rev" and job_id:
            replace = action.endswith("x")
            match action.removesuffix("x"):
                case "save":
                    answer = await _save_review_draft(job_id)
                case "del":
                    answer = _delete_job(job_id, with_receipt=replace)
                case "rescan":
                    answer = await _rescan_job(job_id, ai_first=False, replace=replace)
                case "ai":
                    answer = await _rescan_job(job_id, ai_first=True, replace=replace)
                case "up":
                    # The next step of the route, starting from the one already
                    # spent — no QR ladder, no repeat of the model that failed.
                    answer = await _rescan_job(job_id, ai_first=True, replace=replace,
                                               escalate=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram callback failed")
        answer = f"Не вдалося: {exc}"

    if answer is None:
        return
    reply = answer if isinstance(answer, BotReply) else BotReply(str(answer))
    if prefix == "tri" and prompt_id is not None:
        try:
            await _edit_message(token, chat_id, prompt_id, html.escape(reply.text), reply.keyboard)
        except Exception:  # noqa: BLE001 — don't turn a Telegram edit race into a crash
            logger.warning("could not edit shopping checklist message", exc_info=True)
        return
    if prompt_id is not None:
        # Drop the now-stale action buttons so the choice can't be repeated.
        try:
            await _api(token, "editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": prompt_id, "reply_markup": {"inline_keyboard": []},
            })
        except Exception:  # noqa: BLE001
            logger.debug("could not clear Telegram keyboard", exc_info=True)
    if reply.photo:
        await _send_photo(token, chat_id, reply.photo, html.escape(reply.text),
                          reply_to=prompt_id, thread_id=thread_id, keyboard=reply.keyboard)
    else:
        await _send(token, chat_id, html.escape(reply.text), reply_to=prompt_id,
                    thread_id=thread_id, keyboard=reply.keyboard)


async def polling_loop() -> None:
    offset = 0
    while True:
        try:
            with SessionLocal() as db:
                cfg, token = load_config(db), _secret(db, TOKEN_KEY)
            if not cfg.enabled or not token:
                await asyncio.sleep(5)
                continue
            await _ensure_bot_commands(token)
            data = await _api(token, "getUpdates", {
                "offset": offset, "timeout": 45, "allowed_updates": ["message", "callback_query"],
            })
            for update in data.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                if update.get("callback_query"):
                    await handle_callback(token, update)
                else:
                    await handle_update(token, update)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Telegram polling error")
            await asyncio.sleep(5)
