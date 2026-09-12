"""One-off: classify not-yet-categorised product names in receipt history via
the configured AI backend (ROADMAP.md Крок 1 "Разовий бекфіл усієї історії").

Run from backend/:  venv/bin/python -m scripts.backfill_categories [--force]
(or:  PYTHONPATH=. venv/bin/python scripts/backfill_categories.py [--force])

  (no flag)  only the "Без категорії" queue — names nothing has classified yet.
  --force    drop every AI-made category first (manual ones stay), then re-split
             the whole history from scratch.

A no-op (prints names_classified=0) when no AI backend is set up on the
Settings page — new receipts still get categorised going forward and the
scheduler drains the queue once a backend exists.
"""
from __future__ import annotations

import asyncio
import sys

from app.db import SessionLocal
from app.services.categorize import backfill_all


async def main() -> None:
    force = "--force" in sys.argv[1:]
    with SessionLocal() as db:
        result = await backfill_all(db, force=force)
    line = f"names_classified={result['names_classified']} batches={result['batches']}"
    if result.get("reset"):
        r = result["reset"]
        line += f" reset(aliases_cleared={r['aliases_cleared']} items_reapplied={r['items_reapplied']})"
    print(line)


if __name__ == "__main__":
    asyncio.run(main())
