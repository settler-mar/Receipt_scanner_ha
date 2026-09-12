"""One-time migration: rewrite historical timestamps that were stored as Kyiv
wall-clock time mislabelled as UTC into true UTC.

Background: until the timezone fix, every adapter did
`datetime.strptime(...).replace(tzinfo=UTC)` on a store's *local* timestamp, so a
purchase at 11:40 Kyiv landed in the DB as 11:40 UTC. The frontend then rendered
naive strings as browser-local time, so it happened to look right. Now the
backend stores real UTC and the frontend renders explicitly in Europe/Kyiv, so
the old rows need a one-off shift.

Only stores whose adapter unambiguously parsed a *naive* local timestamp are
shifted:

    atb, silpo, wog, ukrnafta, epicentrk, upg, brsm, fora

Stores that already stored real UTC (klo, dnipro-m, vencon — their APIs send an
offset) and manual entries (already sent as UTC by the frontend) are left alone.

fozzy is mixed: offline receipts (numeric external_id) came from a UTC field and
are left alone; online receipts ("online:" prefix) were naive local and are
shifted. foxtrot rows with a bare 00:00:00 time were parsed from a date string
(local) and are shifted; anything else was a real `now()` and is left alone.

Idempotent: records completion in a `_tz_migration` table and refuses to run twice.

    python -m scripts.migrate_tz_kyiv          # dry run, prints what would change
    python -m scripts.migrate_tz_kyiv --apply  # make a backup and apply
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.config import settings

KYIV = ZoneInfo("Europe/Kyiv")
SHIFT_STORES = ("atb", "silpo", "wog", "ukrnafta", "epicentrk", "upg", "brsm", "fora")
MIGRATION_ID = "tz_kyiv_2026_08"


def _should_shift(store_code: str, external_id: str, purchased_at: str) -> bool:
    if external_id.startswith("manual:"):
        return False
    if store_code in SHIFT_STORES:
        return True
    if store_code == "fozzy":
        return external_id.startswith("online:")
    if store_code == "foxtrot":
        return purchased_at[11:19] == "00:00:00"
    return False  # klo, dnipro-m, vencon — already real UTC


def _db_path() -> Path:
    url = settings.database_url
    assert url.startswith("sqlite:///"), url
    return Path(url[len("sqlite:///") :])


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _fmt(d: dt.datetime) -> str:
    # match the existing column format: "YYYY-MM-DD HH:MM:SS.ffffff"
    return d.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _kyiv_wallclock_to_utc(value: str) -> str:
    d = _parse(value)
    if d.tzinfo is None:
        d = d.replace(tzinfo=KYIV)
    return _fmt(d.astimezone(dt.timezone.utc))


def main() -> None:
    apply = "--apply" in sys.argv
    path = _db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    done = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_tz_migration'"
    ).fetchone()
    if done and conn.execute(
        "SELECT 1 FROM _tz_migration WHERE id = ?", (MIGRATION_ID,)
    ).fetchone():
        print(f"Migration {MIGRATION_ID} already applied — nothing to do.")
        return

    receipts = conn.execute(
        "SELECT id, store_code, external_id, purchased_at FROM receipts"
    ).fetchall()

    to_shift: list[tuple[str, str, str]] = []  # (id, old, new)
    shift_receipt_ids: set[str] = set()
    for r in receipts:
        if not _should_shift(r["store_code"], r["external_id"], r["purchased_at"]):
            continue
        shift_receipt_ids.add(r["id"])
        new = _kyiv_wallclock_to_utc(r["purchased_at"])
        if new != r["purchased_at"]:
            to_shift.append((r["id"], r["purchased_at"], new))

    item_rows = conn.execute(
        """SELECT ri.id, ri.receipt_id, ri.warranty_expires_at
           FROM receipt_items ri WHERE ri.warranty_expires_at IS NOT NULL"""
    ).fetchall()
    items_to_shift = [
        (ir["id"], ir["warranty_expires_at"], _kyiv_wallclock_to_utc(ir["warranty_expires_at"]))
        for ir in item_rows
        if ir["receipt_id"] in shift_receipt_ids
        and _kyiv_wallclock_to_utc(ir["warranty_expires_at"]) != ir["warranty_expires_at"]
    ]

    print(f"receipts.purchased_at to shift: {len(to_shift)}")
    for _id, old, new in to_shift[:5]:
        print(f"  {old}  ->  {new}")
    if len(to_shift) > 5:
        print(f"  ... (+{len(to_shift) - 5} more)")
    print(f"receipt_items.warranty_expires_at to shift: {len(items_to_shift)}")
    left = [r for r in receipts if r["id"] not in shift_receipt_ids and not r["external_id"].startswith("manual:")]
    print(f"rows left unchanged (already real UTC — klo/dnipro-m/vencon/fozzy-offline/foxtrot-now): {len(left)}")

    if not apply:
        print("\nDry run. Re-run with --apply to make a backup and write changes.")
        return

    backup = path.with_suffix(path.suffix + f".bak-{dt.datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(path, backup)
    print(f"\nBackup: {backup}")

    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _tz_migration (id TEXT PRIMARY KEY, applied_at TEXT)"
        )
        conn.executemany(
            "UPDATE receipts SET purchased_at = ? WHERE id = ?",
            [(new, _id) for _id, _old, new in to_shift],
        )
        conn.executemany(
            "UPDATE receipt_items SET warranty_expires_at = ? WHERE id = ?",
            [(new, _id) for _id, _old, new in items_to_shift],
        )
        conn.execute(
            "INSERT INTO _tz_migration (id, applied_at) VALUES (?, ?)",
            (MIGRATION_ID, dt.datetime.now(dt.timezone.utc).isoformat()),
        )
    print(f"Applied. Shifted {len(to_shift)} receipts, {len(items_to_shift)} warranty dates.")


if __name__ == "__main__":
    main()
