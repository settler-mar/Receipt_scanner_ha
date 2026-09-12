"""Telegram bot: receipt ingestion and a small chat facade over MCP features."""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import html
import ipaddress
import logging
import re
import time
import uuid
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
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


@dataclasses.dataclass
class BotReply:
    """A bot answer that may carry an inline keyboard of follow-up actions."""

    text: str
    keyboard: list[list[dict]] | None = None


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
                     rescan: bool = True, save: bool = True) -> list[list[dict]]:
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
        if result.receipt_id:
            receipt = db.get(Receipt, result.receipt_id)
            return _saved_receipt_reply(receipt, result, raw.get("qr_state"),
                                        raw.get("fiscal_url"))

    draft = raw.get("draft") or {}
    if not draft:
        return BotReply(
            "Жодне з фото не вдалося розпізнати. "
            "Спробуйте зняти чек ще раз, з перекриттям між кадрами.",
            keyboard=_review_keyboard(result.scan_job_id, save=False),
        )
    seams = [n for n in (raw.get("notes") or []) if "спільних" in n or "розрив" in n
             or "загубилася" in n]
    return BotReply(
        f"🧩 Склеєно з {raw.get('parts', sent)} фото.\n"
        + ("\n".join(seams) + "\n\n" if seams else "\n")
        + format_draft_message(draft)
        + "\n\nПеревірте позиції: якщо чогось бракує — додайте ще одне фото "
          "з перекриттям.",
        keyboard=_review_keyboard(result.scan_job_id),
    )


async def _scan_bytes(data: bytes, media_type: str, filename: str, *, chat_id: int | None = None) -> "str | BotReply":
    # Import lazily to avoid coupling app startup to the API module graph.
    from app.api.inbox import _handle_receipt

    from app.scanner.fiscal_qr import parse_fiscal_url

    with SessionLocal() as db:
        cfg = load_config(db)
        pending_url = cfg.pending_fiscal_urls.get(str(chat_id)) if chat_id is not None else None
        result = await _handle_receipt(
            db, data, filename, media_type,
            fiscal_override=parse_fiscal_url(pending_url) if pending_url else None,
        )
        from app.models.scanner import ScanJob
        scan_job = db.get(ScanJob, result.scan_job_id)
        raw = (scan_job.raw_ocr or {}) if scan_job else {}
        fiscal_url, qr_state = raw.get("fiscal_url"), raw.get("qr_state")
        if not result.receipt_id:
            from app.services.fiscal_queue import enqueue
            if fiscal_url:
                enqueue(db, fiscal_url, source="telegram", chat_id=chat_id, scan_job_id=result.scan_job_id)
        if result.receipt_id:
            if pending_url:
                cfg.pending_fiscal_urls.pop(str(chat_id), None)
                save_config(db, cfg)
            from app.models.models import Receipt
            receipt = db.get(Receipt, result.receipt_id)
            return _saved_receipt_reply(receipt, result, qr_state, fiscal_url)
    qr_line = _qr_status_line(qr_state, fiscal_url)
    retake = "📷 Надішліть чіткіше фото чека у відповідь на це повідомлення."
    if result.needs_review:
        return BotReply(
            f"🟡 Чек розпізнано частково · метод {result.method}.\n"
            f"{qr_line}\n\n"
            f"{format_draft_message(raw.get('draft') or {})}\n"
            f"{retake}",
            keyboard=_review_keyboard(result.scan_job_id),
        )
    return BotReply(
        "Не вдалося надійно розпізнати чек.\n"
        f"{qr_line}\n{retake}",
        keyboard=_review_keyboard(result.scan_job_id, save=False),
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


def _saved_receipt_reply(receipt, result, qr_state, fiscal_url) -> "BotReply":
    """A saved receipt, with the rescan actions still attached.

    Even a receipt that filed cleanly can be a misread, so the rerun buttons
    stay available; the QR outcome is said out loud too — a decoded ДПС link
    tells the user the data is fiscal, and an unread QR tells them that
    reshooting it upgrades the guess to the ДПС document itself.
    """
    from app.scanner.fiscal_qr import QR_UNREADABLE

    text = format_receipt_message(receipt, duplicate=result.duplicate, method=result.method)
    if fiscal_url or qr_state == QR_UNREADABLE:
        text += f"\n\n{_qr_status_line(qr_state, fiscal_url)}"
    return BotReply(text, keyboard=_review_keyboard(result.scan_job_id, saved=True))


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


async def _rescan_job(job_id: str, *, ai_first: bool, replace: bool = False) -> "str | BotReply":
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
                result = await series_scan.rescan_series(db, job, ai_first=ai_first)
            else:
                result = await scan_jobs.rescan_job(db, job_id, ai_first=ai_first,
                                                    replace=replace)
        except (scan_jobs.ScanJobError, ValueError) as exc:
            return str(exc)

        raw = (db.get(ScanJob, result.scan_job_id).raw_ocr or {})
        if result.receipt_id:
            receipt = db.get(Receipt, result.receipt_id)
            reply = _saved_receipt_reply(receipt, result, raw.get("qr_state"), raw.get("fiscal_url"))
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
            f"{head}\n{_qr_status_line(raw.get('qr_state'), raw.get('fiscal_url'))}\n{body}\n"
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


def _mcp_command(text: str) -> str | None:
    """Reuse the same functions exposed by MCP, without an internal HTTP hop."""
    from app.mcp_server import add_to_shopping_list, list_receipts, search_products, spending_summary

    command, _, arg = text.partition(" ")
    arg = arg.strip()
    if command == "/find":
        if not arg:
            return "Формат: /find молоко"
        rows = search_products(arg)[:8]
        return "\n".join(f"• {r.get('name')} — {r.get('last_price')} грн" for r in rows) or "Нічого не знайдено"
    if command == "/spending":
        value = spending_summary(group_by="total")
        return f"Витрати: {value.get('total_spent', 0)} грн · чеків: {value.get('receipt_count', 0)}"
    if command == "/receipts":
        value = list_receipts(limit=10)
        return "\n".join(f"• {(r.get('purchased_at') or '')[:10]} · {r.get('shop_name') or r.get('store_name')} · {r.get('total_amount')} грн" for r in value["items"]) or "Чеків ще немає"
    if command == "/add":
        if not arg:
            return "Формат: /add молоко"
        value = add_to_shopping_list(arg)
        return f"✅ Додано до списку: {value.get('raw_text') or arg}"
    return None


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
    """Keep, next to the recording, one line about what it turned into."""
    from app.services import voice as voice_svc

    with SessionLocal() as db:
        row = voice_svc.get(db, voice_id)
        if row is not None:
            voice_svc.note_applied(db, row, reply_text.splitlines()[0][:200])


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
            rows = capture.add_shopping_items(db, draft)
            batch = _remember_capture_batch(db, [row.id for row in rows])
            lines = [f"🛒 Додано до списку ({len(rows)}):"]
            for row in rows:
                qty = f" — {float(row.qty):g}{(' ' + row.unit) if row.unit else ''}" if row.qty and float(row.qty) != 1 else ""
                note = f" ({row.note})" if row.note else ""
                lines.append(f"• {row.raw_text}{qty}{note}")
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
                      "Команди: /find товар, /spending, /receipts, /add товар.")
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
                message_id=reply_to, chat_id=chat_id)
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
                                               chat_id=chat_id)
        elif message.get("document") and str(message["document"].get("mime_type", "")).startswith("image/"):
            doc = message["document"]
            await _ack(token, chat_id, reply_to, thread_id)
            data, media = await _download_telegram_file(token, doc["file_id"])
            answer = await _scan_bytes(data, media, doc.get("file_name") or "telegram-image", chat_id=chat_id)
        elif (url_match := _URL_RE.search(text)):
            await _ack(token, chat_id, reply_to, thread_id)
            answer = await _scan_url(url_match.group(0).rstrip(".,)"), chat_id=chat_id)
        else:
            # Slash commands and links first; only a message that is none of
            # those is worth spending an AI call on.
            answer = _mcp_command(text)
            if answer is None and text:
                await _ack(token, chat_id, reply_to, thread_id, "🧠 Читаю повідомлення…")
                answer = await _capture_reply(text)
            if answer is None:
                answer = ("Надішліть фото, QR або посилання на чек — або просто напишіть "
                          "«додай чек ...» / «додай в список покупок ...». /help — команди.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram update failed")
        answer = f"Не вдалося обробити: {exc}"
        finish_incoming_message(log_id, status="error", error=str(exc))
    reply = answer if isinstance(answer, BotReply) else BotReply(str(answer))
    if not str(answer).startswith("Не вдалося обробити:"):
        finish_incoming_message(log_id, status="processed", outcome=reply.text)
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

    answer: "str | BotReply | None" = None
    try:
        # "rev:<action>:<scan job id>"; an action ending in "x" replaces the
        # receipt this scan already produced.
        prefix, _, rest = data.partition(":")
        action, _, job_id = rest.partition(":")
        if prefix == "cap" and action == "undo" and job_id:
            answer = _undo_capture_batch(job_id)
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram callback failed")
        answer = f"Не вдалося: {exc}"

    await _api(token, "answerCallbackQuery", {"callback_query_id": query_id})
    if answer is None:
        return
    reply = answer if isinstance(answer, BotReply) else BotReply(str(answer))
    if prompt_id is not None:
        # Drop the now-stale action buttons so the choice can't be repeated.
        try:
            await _api(token, "editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": prompt_id, "reply_markup": {"inline_keyboard": []},
            })
        except Exception:  # noqa: BLE001
            logger.debug("could not clear Telegram keyboard", exc_info=True)
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
