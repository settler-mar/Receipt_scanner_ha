"""File-backed storage for provider responses used while debugging imports."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import BASE_DIR
from app.models.models import AppSetting

logger = logging.getLogger(__name__)

CONFIG_KEY = "raw_response_storage"
RAW_ROOT = BASE_DIR / "data" / "raw_responses"
DEFAULT_RETENTION_DAYS = 90


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def load_settings(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, CONFIG_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    try:
        retention_days = max(1, int(value.get("retention_days", DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        retention_days = DEFAULT_RETENTION_DAYS
    return {
        "retention_days": retention_days,
        "auto_cleanup": bool(value.get("auto_cleanup", False)),
    }


def save_settings(db: Session, retention_days: int, auto_cleanup: bool) -> dict[str, Any]:
    value = {"retention_days": max(1, int(retention_days)), "auto_cleanup": bool(auto_cleanup)}
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=value))
    else:
        row.value = value
    db.commit()
    return value


def _safe_part(value: str | None, fallback: str = "unknown") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return text[:120] or fallback


def _as_utc(value: dt.datetime | None) -> dt.datetime:
    if value is None:
        return _now()
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def store(
    *,
    store_code: str,
    account_id: str,
    collection_run_id: str,
    external_id: str,
    payload: Any,
    captured_at: dt.datetime | None = None,
) -> Path | None:
    """Write one provider response entry and return its path.

    The payload is wrapped with import metadata so a response can be traced back
    to an account and collection run without opening the normalized receipt.
    """
    if payload in (None, {}, []):
        return None
    captured = _as_utc(captured_at)
    folder = RAW_ROOT / _safe_part(store_code) / captured.strftime("%Y") / captured.strftime("%m")
    folder.mkdir(parents=True, exist_ok=True)
    envelope = {
        "store_code": store_code,
        "account_id": account_id,
        "collection_run_id": collection_run_id,
        "external_id": external_id,
        "captured_at": captured.isoformat(),
        "payload": payload,
    }
    path = folder / (
        f"{captured.strftime('%Y%m%dT%H%M%S%fZ')}_"
        f"{_safe_part(external_id)}_{uuid.uuid4().hex[:8]}.json"
    )
    path.write_text(
        json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    return path


def _iter_files():
    if not RAW_ROOT.exists():
        return
    yield from (path for path in RAW_ROOT.rglob("*.json") if path.is_file())


def _file_meta(path: Path) -> tuple[str, dt.datetime | None]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        store_code = str(data.get("store_code") or path.relative_to(RAW_ROOT).parts[0])
        captured = data.get("captured_at")
        when = dt.datetime.fromisoformat(captured.replace("Z", "+00:00")) if captured else None
        return store_code, when
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return (path.relative_to(RAW_ROOT).parts[0] if path.is_relative_to(RAW_ROOT) else "unknown", None)


def summary() -> dict[str, Any]:
    stores: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    total_count = 0
    for path in _iter_files() or ():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        code, captured = _file_meta(path)
        row = stores.setdefault(code, {"store_code": code, "count": 0, "bytes": 0, "oldest_at": None, "newest_at": None})
        row["count"] += 1
        row["bytes"] += size
        total_count += 1
        total_bytes += size
        if captured is not None:
            iso = captured.isoformat()
            row["oldest_at"] = min(row["oldest_at"], iso) if row["oldest_at"] else iso
            row["newest_at"] = max(row["newest_at"], iso) if row["newest_at"] else iso
    return {
        "total_count": total_count,
        "total_bytes": total_bytes,
        "stores": sorted(stores.values(), key=lambda row: row["store_code"]),
    }


def cleanup_all() -> int:
    if not RAW_ROOT.exists():
        return 0
    count = sum(1 for path in _iter_files() or ())
    shutil.rmtree(RAW_ROOT)
    return count


def cleanup_older_than(retention_days: int) -> int:
    cutoff = _now() - dt.timedelta(days=max(1, int(retention_days)))
    removed = 0
    for path in list(_iter_files() or ()):
        _, captured = _file_meta(path)
        if captured is not None and _as_utc(captured) < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.warning("Could not remove raw response %s", path, exc_info=True)
    # Remove empty month/store folders, but never the data root itself.
    if RAW_ROOT.exists():
        for folder in sorted(RAW_ROOT.rglob("*"), reverse=True):
            if folder.is_dir():
                try:
                    folder.rmdir()
                except OSError:
                    pass
    return removed


def maybe_cleanup(db: Session) -> int:
    config = load_settings(db)
    return cleanup_older_than(config["retention_days"]) if config["auto_cleanup"] else 0
