"""Unified operational journal — one page for logs that live in six places.

Every subsystem already records its own history in the shape that suits it:
collection runs, scan jobs, duplicate runs, Telegram messages, the AI ring
buffer in `app_settings`, the fiscal queue. Rather than migrating all of them
into one table (and losing their specialised screens), this endpoint *reads*
them all, maps each row onto one common line, and merges the result by time.
Events with no home of their own are written to `app_events` by
`services/event_log.py` and merged in the same way.

Read-only: nothing here writes or deletes.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.registry import get_adapter
from app.core.time import as_utc
from app.db import get_db
from app.models.models import (
    Account, AccountStatus, AppEvent, CollectionRun, DuplicateRun,
    DuplicateRunStatus, RunStatus, Store, TelegramMessageLog,
)
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus, ScanJob, ScanStatus
from app.services import ai, fiscal_queue

router = APIRouter(prefix="/api/logs", tags=["logs"])

# code -> label, in display order. `app_events` rows carry their own source, so
# anything written there must use one of these codes.
SOURCES: tuple[tuple[str, str], ...] = (
    ("collector", "Збір чеків"),
    ("scanner", "Сканер чеків"),
    ("fiscal", "Черга ДПС"),
    ("shopping", "Список покупок"),
    ("ai", "AI"),
    ("telegram", "Telegram"),
    ("duplicates", "Дублі"),
    ("scheduler", "Планувальник"),
    ("system", "Система"),
)
_LABELS = dict(SOURCES)

INFO, WARNING, ERROR = "info", "warning", "error"
_PER_SOURCE_CAP = 500


class LogEntry(BaseModel):
    id: str
    ts: dt.datetime
    source: str
    source_name: str
    level: str
    title: str
    message: str | None = None
    details: dict = {}
    # In-app hash route for the record this line is about, when there is one.
    link: str | None = None


class Problem(BaseModel):
    """One thing that is broken *right now* (not "was broken at some point")."""

    id: str
    kind: str                    # account | scanner | fiscal | duplicates | event
    source: str
    source_name: str
    level: str
    title: str
    message: str | None = None
    since: dt.datetime | None = None
    link: str | None = None


class Problems(BaseModel):
    total: int
    accounts: int                # how many integrations are currently failing
    items: list[Problem]


class LogPage(BaseModel):
    items: list[LogEntry]
    has_more: bool
    sources: list[dict]


def _entry(**kw) -> dict:
    kw.setdefault("source_name", _LABELS.get(kw["source"], kw["source"]))
    return kw


def _collector(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(CollectionRun, Account, Store)
        .join(Account, Account.id == CollectionRun.account_id)
        .join(Store, Store.id == Account.store_id)
        .order_by(CollectionRun.started_at.desc()).limit(cap)
    ).all()
    out = []
    for run, account, store in rows:
        level = ERROR if run.status == RunStatus.error else (
            WARNING if run.status == RunStatus.partial else INFO)
        out.append(_entry(
            id=f"run:{run.id}", ts=as_utc(run.finished_at or run.started_at),
            source="collector", level=level,
            title=f"{store.name} · {account.label}",
            message=run.error_message or (
                f"Знайдено {run.receipts_found}, нових {run.receipts_new}"),
            details={"trigger": run.trigger, "status": run.status.value,
                     "receipts_found": run.receipts_found, "receipts_new": run.receipts_new},
            link=f"/accounts/{account.id}",
        ))
    return out


def _scanner(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(ScanJob).order_by(ScanJob.created_at.desc()).limit(cap)
    ).scalars().all()
    return [_entry(
        id=f"scan:{job.id}", ts=as_utc(job.created_at), source="scanner",
        level=ERROR if job.status.value == "failed" else (
            WARNING if job.status.value == "needs_review" else INFO),
        title=job.source_filename or "Скан чека",
        message=job.error or f"Метод: {job.method or '—'}, статус: {job.status.value}",
        details={"status": job.status.value, "method": job.method,
                 "confidence": job.confidence, "receipt_id": job.receipt_id},
        link="/inbox",
    ) for job in rows]


def _fiscal(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(FiscalQueueJob).order_by(FiscalQueueJob.created_at.desc()).limit(cap)
    ).scalars().all()
    return [_entry(
        id=f"fiscal:{job.id}", ts=as_utc(job.created_at), source="fiscal",
        level=ERROR if job.status.value == "failed" else (
            WARNING if job.status.value == "captcha" else INFO),
        title=f"Чек ДПС · {job.status.value}",
        message=job.error or job.url,
        details={"status": job.status.value, "attempts": job.attempts,
                 "receipt_id": job.receipt_id},
        link="/inbox",
    ) for job in rows]


def _duplicates(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(DuplicateRun).order_by(DuplicateRun.started_at.desc()).limit(cap)
    ).scalars().all()
    return [_entry(
        id=f"dup:{run.id}", ts=as_utc(run.finished_at or run.started_at),
        source="duplicates",
        level=ERROR if run.status.value == "error" else INFO,
        title=f"Пошук дублів ({run.trigger})",
        message=run.error_message or (
            f"Перевірено {run.scanned_receipts}, нових груп {run.candidates_new}"),
        details={"status": run.status.value, "scanned": run.scanned_receipts,
                 "candidates_found": run.candidates_found,
                 "candidates_new": run.candidates_new},
        link="/duplicates",
    ) for run in rows]


def _telegram(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(TelegramMessageLog).order_by(TelegramMessageLog.received_at.desc()).limit(cap)
    ).scalars().all()
    return [_entry(
        id=f"tg:{row.id}", ts=as_utc(row.received_at), source="telegram",
        level=ERROR if row.error else (INFO if row.authorized else WARNING),
        title=f"{row.sender_display_name or row.sender_username or row.chat_id}",
        message=row.error or row.outcome or (row.text or row.content_type),
        details={"status": row.status, "authorized": row.authorized,
                 "content_type": row.content_type, "chat_id": row.chat_id},
    ) for row in rows]


def _ai(db: Session, cap: int) -> list[dict]:
    out = []
    for idx, row in enumerate(ai.recent_log(db)[:cap]):
        try:
            ts = as_utc(dt.datetime.fromisoformat(row["ts"]))
        except (KeyError, TypeError, ValueError):
            continue
        out.append(_entry(
            id=f"ai:{row['ts']}:{idx}", ts=ts, source="ai",
            level=INFO if row.get("ok") else ERROR,
            title=f"{row.get('task') or 'AI'} · {row.get('backend') or '—'}",
            message=row.get("error") or row.get("note") or "",
            details={k: row.get(k) for k in
                     ("model", "tokens_in", "tokens_out", "cached_tokens", "duration_ms")},
            link="/settings/ai-providers",
        ))
    return out


def _events(db: Session, cap: int) -> list[dict]:
    rows = db.execute(
        select(AppEvent).order_by(AppEvent.created_at.desc()).limit(cap)
    ).scalars().all()
    return [_entry(
        id=f"ev:{row.id}", ts=as_utc(row.created_at), source=row.source,
        level=row.level, title=row.title, message=row.message,
        details=row.details or {},
    ) for row in rows]


# `app_events` can carry any source code, so it is always collected and then
# filtered by the requested source like every other line.
_READERS = {
    "collector": _collector, "scanner": _scanner, "fiscal": _fiscal,
    "duplicates": _duplicates, "telegram": _telegram, "ai": _ai,
}


@router.get("", response_model=LogPage)
def list_logs(
    source: str | None = Query(default=None, description="код источника, см. sources"),
    level: str | None = Query(default=None, pattern="^(info|warning|error)$"),
    q: str | None = Query(default=None, description="подстрока в заголовке/сообщении"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Merged journal, newest first. `source=all` (or omitted) reads everything."""
    cap = min(_PER_SOURCE_CAP, limit + offset + 1)
    wanted = None if source in (None, "", "all") else source

    entries: list[dict] = _events(db, cap)
    for code, reader in _READERS.items():
        if wanted in (None, code):
            entries.extend(reader(db, cap))

    if wanted:
        entries = [e for e in entries if e["source"] == wanted]
    if level:
        entries = [e for e in entries if e["level"] == level]
    if q:
        needle = q.casefold()
        entries = [e for e in entries
                   if needle in (e["title"] or "").casefold()
                   or needle in (e["message"] or "").casefold()]

    entries.sort(key=lambda e: e["ts"], reverse=True)
    page = entries[offset:offset + limit]
    return LogPage(
        items=[LogEntry(**e) for e in page],
        has_more=len(entries) > offset + limit,
        sources=[{"code": code, "name": name} for code, name in SOURCES],
    )


# --------------------------------------------------------------------------- #
# Active problems — what the sidebar badges count
# --------------------------------------------------------------------------- #
def _account_problems(db: Session) -> list[dict]:
    """An integration is a problem while its *latest* run is a failure, or while
    it is waiting for a new login. A successful run afterwards resolves it — the
    history stays in the journal, but it stops being counted."""
    rows = db.execute(
        select(Account, Store).join(Store, Store.id == Account.store_id)
    ).all()
    out = []
    for account, store in rows:
        # Manual stores (сканер/OCR) have nothing to collect and nothing to log
        # into — the scheduler skips them, so an ancient "Немає токена" run must
        # not sit in the problem list forever.
        if not get_adapter(store.code).capabilities["receipts"]:
            continue
        last = db.execute(
            select(CollectionRun)
            .where(CollectionRun.account_id == account.id,
                   CollectionRun.status != RunStatus.running)
            .order_by(CollectionRun.started_at.desc()).limit(1)
        ).scalars().first()
        needs_login = account.status in (AccountStatus.needs_reauth, AccountStatus.error)
        # A failed run remains in the journal, but reset-error explicitly
        # acknowledges the current incident. Do not keep advertising it as an
        # active problem after that acknowledgement.
        last_failed = (
            last is not None
            and last.status == RunStatus.error
            and account.last_error is not None
        )
        if not needs_login and not last_failed:
            continue
        out.append(_entry(
            id=f"account:{account.id}", kind="account", source="collector",
            level=ERROR,
            title=f"{store.name} · {account.label}",
            message=(account.last_error
                     or (last.error_message if last_failed else None)
                     or "Потрібен повторний вхід"),
            since=as_utc(last.finished_at or last.started_at) if last else None,
            link=f"/accounts/{account.id}",
        ))
    return out


def _open_job_problems(db: Session) -> list[dict]:
    """Uploads and ДПС jobs that are still stuck — an imported/parsed job is done
    and never counted, however it went."""
    out = []
    failed_scans = db.execute(
        select(ScanJob).where(ScanJob.status == ScanStatus.failed,
                              ScanJob.archived_at.is_(None))
        .order_by(ScanJob.created_at.desc())
    ).scalars().all()
    for job in failed_scans:
        out.append(_entry(
            id=f"scan:{job.id}", kind="scanner", source="scanner", level=ERROR,
            title=job.source_filename or "Скан чека",
            message=job.error or "Не вдалося розпізнати чек",
            since=as_utc(job.created_at), link="/inbox",
        ))
    for job in fiscal_queue.open_jobs(db):
        out.append(_entry(
            id=f"fiscal:{job.id}", kind="fiscal", source="fiscal",
            level=ERROR if job.status == FiscalQueueStatus.failed else WARNING,
            title=f"Чек ДПС · {job.status.value}",
            message=job.error or job.url,
            since=as_utc(job.created_at), link="/inbox",
        ))
    return out


def _latest_run_problems(db: Session) -> list[dict]:
    """Background jobs whose most recent run failed."""
    run = db.execute(
        select(DuplicateRun).where(DuplicateRun.status != DuplicateRunStatus.running)
        .order_by(DuplicateRun.started_at.desc()).limit(1)
    ).scalars().first()
    if run is None or run.status != DuplicateRunStatus.error:
        return []
    return [_entry(
        id=f"dup:{run.id}", kind="duplicates", source="duplicates", level=ERROR,
        title="Пошук дублів", message=run.error_message,
        since=as_utc(run.finished_at or run.started_at), link="/duplicates",
    )]


def _latest_event_problems(db: Session) -> list[dict]:
    """`app_events` has no "resolved" state, so only the newest event per source
    counts: a failed automatic run that succeeded next time is history."""
    out = []
    sources = db.execute(select(AppEvent.source).distinct()).scalars().all()
    for source in sources:
        row = db.execute(
            select(AppEvent).where(AppEvent.source == source)
            .order_by(AppEvent.created_at.desc()).limit(1)
        ).scalars().first()
        if row is None or row.level not in (ERROR, WARNING):
            continue
        out.append(_entry(
            id=f"ev:{row.id}", kind="event", source=row.source, level=row.level,
            title=row.title, message=row.message, since=as_utc(row.created_at),
            link="/logs",
        ))
    return out


def active_problems(db: Session) -> list[dict]:
    """Everything that is broken right now, newest and most severe first."""
    accounts = _account_problems(db)
    items = accounts + _open_job_problems(db) + _latest_run_problems(db) + _latest_event_problems(db)
    items.sort(key=lambda e: (e["level"] != ERROR, -(e["since"].timestamp() if e["since"] else 0)))
    return items


def active_problem_counts(db: Session) -> tuple[int, int]:
    """(всего проблем, из них — сломанных интеграций) для бейджей навигации."""
    items = active_problems(db)
    return len(items), sum(1 for e in items if e["kind"] == "account")


@router.get("/problems", response_model=Problems)
def list_problems(db: Session = Depends(get_db)):
    """What is broken right now — the source of truth for the sidebar badges.

    Deliberately *not* "errors in the last 24 hours": a failure that has since
    been fixed (a re-login, a successful re-run, an imported receipt) must stop
    being advertised. Transient AI/Telegram call errors are history by nature
    and are never counted; they stay visible in the journal itself.
    """
    items = active_problems(db)
    return Problems(total=len(items),
                    accounts=sum(1 for e in items if e["kind"] == "account"),
                    items=[Problem(**e) for e in items])
