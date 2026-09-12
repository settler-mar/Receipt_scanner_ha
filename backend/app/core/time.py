"""SQLite doesn't persist tzinfo on DateTime(timezone=True) columns — values come
back naive even though everything is written/compared as UTC. Every value read
from the DB and compared against a fresh `datetime.now(timezone.utc)` needs this.

Storage/comparison is always UTC. The only place local time matters is the
collection Schedule: its cron expression ("0 12,19 * * *") is what the user typed
looking at a Kyiv clock, so it must be evaluated in Europe/Kyiv, not UTC —
otherwise every schedule fires 2-3h late (whatever the current Kyiv↔UTC offset
is). See scheduler.py.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

# The app is a single-user LAN tool for one household in Ukraine; there is no
# per-account timezone, so this is a hard-coded constant rather than config.
KYIV = ZoneInfo("Europe/Kyiv")


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def store_local_to_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a timestamp coming from a Ukrainian store's API or a printed
    receipt to aware UTC.

    Most stores send wall-clock time with no timezone (e.g. "2026-08-20T20:21:41",
    "27.08.2026 11:40:33") — that is Kyiv local time, not UTC, so it gets
    Europe/Kyiv attached and is converted. Values that already carry an offset
    (KLO, some Fozzy/UPG rows) are just converted straight to UTC.

    Everything downstream (DB storage, `since` comparisons, display) is then
    true UTC — see the module docstring.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=KYIV)
    return value.astimezone(dt.timezone.utc)


def next_cron_run(cron_expression: str, after: dt.datetime) -> dt.datetime:
    """Next fire time for a 5-field cron expression, interpreted in Kyiv local
    time, returned as an aware UTC datetime ready to store/compare."""
    from croniter import croniter

    base = as_utc(after).astimezone(KYIV)
    nxt = croniter(cron_expression, base).get_next(dt.datetime)
    return nxt.astimezone(dt.timezone.utc)
