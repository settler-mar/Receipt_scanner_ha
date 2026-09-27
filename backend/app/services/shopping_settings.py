"""User settings of the shopping list (SHOPPING_LIST_WORKPLAN.md).

Stored in the generic `app_settings` KV row `shopping_list`, like the other
small feature configs (see mcp_config.py). Currently one feature: run the
category classification automatically once the list has been quiet for a while,
so the user does not have to press "Класифікувати без категорії" at all.

`state` is not user input — it records which addition the automatic pass has
already handled, so a quiet list is not re-classified every minute.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.models.models import AppSetting

CONFIG_KEY = "shopping_list"

DEFAULTS: dict = {
    "auto_categorize": True,
    # Minutes of quiet after the most recently added item before the automatic
    # pass runs. The user asked for 10; 0 < value <= 1440.
    "auto_categorize_delay_minutes": 10,
}


def load(db: Session) -> dict:
    row = db.get(AppSetting, CONFIG_KEY)
    stored = row.value if row and isinstance(row.value, dict) else {}
    config = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}
    config["auto_categorize"] = bool(config["auto_categorize"])
    config["auto_categorize_delay_minutes"] = max(
        1, min(1440, int(config["auto_categorize_delay_minutes"] or 1))
    )
    return config


def save(db: Session, values: dict) -> dict:
    row = db.get(AppSetting, CONFIG_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    stored.update({k: v for k, v in values.items() if k in DEFAULTS})
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=stored))
    else:
        row.value = stored
    db.commit()
    return load(db)


def last_auto_run_marker(db: Session) -> str | None:
    """`added_at` of the newest item the automatic pass has already seen."""
    row = db.get(AppSetting, CONFIG_KEY)
    stored = row.value if row and isinstance(row.value, dict) else {}
    marker = stored.get("auto_categorize_seen_added_at")
    return marker if isinstance(marker, str) else None


def set_auto_run_marker(db: Session, added_at: dt.datetime) -> None:
    row = db.get(AppSetting, CONFIG_KEY)
    stored = dict(row.value) if row and isinstance(row.value, dict) else {}
    stored["auto_categorize_seen_added_at"] = added_at.isoformat()
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=stored))
    else:
        row.value = stored
    db.commit()
