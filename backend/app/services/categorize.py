"""Receipt-item → category-tree tagging (ROADMAP.md Крок 1).

Two halves:
  * `apply_known_categories` — synchronous, deterministic, runs on every receipt
    ingest: for each item whose normalised name is already in `product_category`,
    stamp `category_id`. No model calls.
  * `run_classification_batch` / `backfill_all` — the background pass: collect
    names nobody has classified yet, run them through `classify_hierarchical`
    (coarse top-level pass, then a per-branch sub-category pass — keeps each
    model prompt small), cache the answers in `product_category`, then fan the
    result out to every item with that name.

A `product_category` row with `source="user"` is sticky — the AI pass never
touches it.
"""
from __future__ import annotations

import datetime as dt
import asyncio
import json
import logging
import re
import threading
from collections import defaultdict

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.models.models import (
    Category,
    ProductCategory,
    ProductCategorySource,
    Receipt,
    ReceiptItem,
)
from app.services import ai
from app.services.text import normalize_name

logger = logging.getLogger(__name__)

# Below this the AI guess is recorded (so the name isn't re-queued) but not
# applied to items — matches ROADMAP.md "впевненість низька" handling.
_MIN_CONFIDENCE = 0.5
_BACKFILL_LOCK = threading.Lock()


def reserve_backfill() -> None:
    if not _BACKFILL_LOCK.acquire(blocking=False):
        raise RuntimeError("Класифікація вже виконується")


def run_backfill_in_background(*, force: bool = False, include_other: bool = False) -> None:
    """Run history classification outside the request event loop."""
    from app.db import SessionLocal

    async def _run() -> None:
        with SessionLocal() as db:
            await backfill_all(db, force=force, include_other=include_other)

    try:
        asyncio.run(_run())
    finally:
        _BACKFILL_LOCK.release()


def _norm_path(p: str) -> str:
    """Loosen a category path for matching: lowercase, trim, collapse the
    separators a model tends to emit (' / ', ' > ', '\\') to a plain '/'."""
    import re
    return re.sub(r"\s*[/>\\|]\s*", "/", (p or "").strip().lower())


def category_id_for_path(db: Session, path: str) -> str | None:
    if not path:
        return None
    hit = db.execute(select(Category.id).where(Category.path == path)).scalar_one_or_none()
    if hit is not None:
        return hit
    # Model output rarely matches the materialised path byte-for-byte — retry loosely.
    target = _norm_path(path)
    for cid, cpath in db.execute(select(Category.id, Category.path)).all():
        if _norm_path(cpath) == target:
            return cid
    return None


def taxonomy_paths(db: Session) -> list[str]:
    return list(
        db.execute(select(Category.path).order_by(Category.path)).scalars().all()
    )


# The app's own designated catch-all root ("Інше" in CATEGORY_TREE — see
# app/seed.py). Hardcoded by path like the category-tree migrations do,
# rather than a settings flag: there's exactly one such bucket per household.
OTHER_CATEGORY_PATH = "inshe"


def other_category_id(db: Session) -> str | None:
    """id of the catch-all "Інше" root, or None if it's been renamed/removed —
    callers then just fall back to the plain uncategorised-only behavior."""
    return db.execute(
        select(Category.id).where(Category.path == OTHER_CATEGORY_PATH, Category.parent_id.is_(None))
    ).scalar_one_or_none()


def _pending_clause(other_id: str | None):
    """Items eligible for (re)classification: no category, or — when the
    "Розібрати й «Інше»" checkbox is on — also filed under the catch-all."""
    if other_id is None:
        return ReceiptItem.category_id.is_(None)
    return or_(ReceiptItem.category_id.is_(None), ReceiptItem.category_id == other_id)


def _in_branch(path: str, root: str) -> bool:
    p, r = _norm_path(path), _norm_path(root)
    return bool(r) and (p == r or p.startswith(r + "/"))


async def classify_hierarchical(
    db: Session,
    names: list[str],
    taxonomy: list[str],
    contexts: dict[str, dict] | None = None,
) -> dict[str, "ai.CategoryGuess"]:
    """Coarse-to-fine classification, to keep every model prompt small
    (ROADMAP.md Крок 1, "Токен-бюджет"):

      1. route each product to a **top-level** category, sending only the root
         labels (~15 strings) alongside the product names;
      2. for each top-level bucket, split its products across just that branch's
         sub-paths — never the whole tree.

    Instead of `len(names) × full_taxonomy` tokens per call it costs
    `len(names) × roots` once + `bucket × branch` per non-empty root. Falls back
    to a single flat call when the tree is only one level deep.
    """
    roots = [p for p in taxonomy if "/" not in p]
    if not roots or not any("/" in p for p in taxonomy):
        return await ai.classify_products(db, names, taxonomy, contexts)

    coarse = await ai.classify_products(db, names, roots, contexts)
    root_by_norm = {_norm_path(r): r for r in roots}

    def _match_root(path: str) -> str | None:
        p = _norm_path(path)
        if not p:
            return None
        if p in root_by_norm:
            return root_by_norm[p]
        # A "too helpful" pass-1 answer may already be a deeper path — snap it
        # back to its root so pass 2 can still refine it.
        for rn, r in root_by_norm.items():
            if p.startswith(rn + "/"):
                return r
        return None

    buckets: dict[str, list[str]] = defaultdict(list)
    result: dict[str, ai.CategoryGuess] = {
        n: ai.CategoryGuess(category_path="", confidence=0.0) for n in names
    }
    for name in names:
        g = coarse.get(name)
        root = _match_root(g.category_path) if g else None
        if root is None:
            continue  # model gave no / an unknown top-level category
        buckets[root].append(name)
        result[name] = ai.CategoryGuess(category_path=root, confidence=g.confidence)

    for root, bucket in buckets.items():
        branch = [p for p in taxonomy if _in_branch(p, root)]
        if len(branch) <= 1:
            continue  # leaf root — the pass-1 answer is already final
        bucket_contexts = (
            {name: contexts[name] for name in bucket if name in contexts}
            if contexts else None
        )
        fine = await ai.classify_products(db, bucket, branch, bucket_contexts)
        for name in bucket:
            fg = fine.get(name)
            coarse_conf = float(coarse[name].confidence) if coarse.get(name) else 0.0
            if fg and fg.category_path and _in_branch(fg.category_path, root):
                result[name] = ai.CategoryGuess(
                    category_path=fg.category_path,
                    confidence=min(coarse_conf, float(fg.confidence)),
                )
            # else: keep the top-level fallback already set above
    return result


def apply_known_categories(db: Session, receipt: Receipt) -> int:
    """Stamp category_id on every item of `receipt` whose name is already known.
    Returns how many items were newly tagged. Caller commits."""
    tagged = 0
    for item in receipt.items:
        if item.category_id is not None:
            continue
        norm = normalize_name(item.name)
        if not norm:
            continue
        row = db.get(ProductCategory, norm)
        if row is not None and row.category_id is not None:
            item.category_id = row.category_id
            tagged += 1
    return tagged


def record_user_choice(db: Session, name: str, category_id: str | None) -> None:
    """A manual category edit in the UI — sticky, wins over any AI guess and is
    never overwritten by `run_classification_batch`. Caller commits."""
    norm = normalize_name(name)
    if not norm:
        return
    row = db.get(ProductCategory, norm)
    if row is None:
        db.add(
            ProductCategory(
                normalized_name=norm,
                category_id=category_id,
                source=ProductCategorySource.user,
                confidence=1.0,
            )
        )
    else:
        row.category_id = category_id
        row.source = ProductCategorySource.user
        row.confidence = 1.0
        row.updated_at = dt.datetime.now(dt.timezone.utc)


def pending_names(db: Session, limit: int, *, include_other: bool = False) -> list[str]:
    """Distinct normalised item names with no category (or, with `include_other`,
    also ones currently sitting in the catch-all "Інше") and no cache row yet.

    Filing something in "Інше" — by hand or by the AI's own "no better fit"
    guess — always writes a `product_category` row, same as a real category
    pick. With `include_other` that row must NOT count as "already handled" —
    otherwise every name ever dumped in the catch-all would silently vanish
    from this queue forever, and the checkbox would do nothing.
    """
    other_id = other_category_id(db) if include_other else None
    known_query = select(ProductCategory.normalized_name)
    if other_id is not None:
        known_query = known_query.where(
            ProductCategory.category_id.is_not(None),
            ProductCategory.category_id != other_id,
        )
    known = set(db.execute(known_query).scalars().all())
    clause = _pending_clause(other_id)
    seen: dict[str, None] = {}
    rows = db.execute(select(ReceiptItem.name).where(clause)).scalars()
    for raw in rows:
        norm = normalize_name(raw)
        if norm and norm not in known and norm not in seen:
            seen[norm] = None
            if len(seen) >= limit:
                break
    return list(seen)


def classification_contexts(db: Session, names: list[str], *, include_other: bool = False) -> dict[str, dict]:
    """Receipt context for a classification batch, keyed by normalised name.

    A product alias remains global, but the model now sees the shop(s) where the
    still-uncategorised name occurred. Multiple shops are retained because the
    same abbreviated receipt label can legitimately appear in more than one.
    """
    wanted = set(names)
    stores: dict[str, list[str]] = defaultdict(list)
    clause = _pending_clause(other_category_id(db) if include_other else None)
    rows = db.execute(
        select(ReceiptItem.name, Receipt.shop_name, Receipt.store_code)
        .join(Receipt, Receipt.id == ReceiptItem.receipt_id)
        .where(clause)
    ).all()
    for raw_name, shop_name, store_code in rows:
        norm = normalize_name(raw_name)
        if norm not in wanted:
            continue
        label = (shop_name or "").strip() or (store_code or "").strip()
        if label and label not in stores[norm]:
            stores[norm].append(label)
    return {
        name: {"store_names": labels[:5]}
        for name, labels in stores.items()
        if labels
    }


def _apply_cached_rows(db: Session, norms: list[str], *, include_other: bool = False) -> int:
    """Set category_id on every pending item (uncategorised, or — with
    `include_other` — currently in "Інше") whose normalised name maps to a
    cache row that has a category. Done name-by-name in Python because the
    normalisation isn't expressible in SQL."""
    applied = 0
    resolved = {
        r.normalized_name: r.category_id
        for r in db.execute(
            select(ProductCategory).where(
                ProductCategory.normalized_name.in_(norms),
                ProductCategory.category_id.is_not(None),
            )
        ).scalars()
    }
    if not resolved:
        return 0
    clause = _pending_clause(other_category_id(db) if include_other else None)
    items = db.execute(select(ReceiptItem).where(clause)).scalars()
    for item in items:
        cat_id = resolved.get(normalize_name(item.name))
        if cat_id is not None:
            item.category_id = cat_id
            applied += 1
    return applied


async def run_classification_batch(db: Session, limit: int = 50, *, include_other: bool = False) -> int:
    """One batch: classify up to `limit` unknown (or, with `include_other`, also
    "Інше") names, cache + apply them. Returns the number of names processed
    (0 when the queue is empty or no AI backend is configured)."""
    names = pending_names(db, limit, include_other=include_other)
    if not names:
        return 0
    taxonomy = taxonomy_paths(db)
    if not taxonomy:
        logger.warning("classification skipped: category tree is empty")
        return 0

    try:
        guesses = await classify_hierarchical(
            db, names, taxonomy, classification_contexts(db, names, include_other=include_other)
        )
    except ai.AiConfigError as exc:
        logger.info("classification skipped: %s", exc)
        return 0
    except Exception:  # noqa: BLE001 — a bad batch must not kill the scheduler
        logger.warning("classification batch failed", exc_info=True)
        return 0

    counts = apply_guesses(db, {n: guesses.get(n) for n in names}, backend="", include_other=include_other)
    return counts["names"]


async def classify_receipt_item(
    db: Session,
    receipt: Receipt,
    item: ReceiptItem,
    clarification: str,
) -> str | None:
    """Reclassify one concrete receipt line using a user's free-text hint.

    This intentionally updates only the selected item. It does not rewrite the
    global name alias, because a clarification such as "this is car shampoo"
    may only be true for this otherwise ambiguous line.
    """
    norm = normalize_name(item.name)
    taxonomy = taxonomy_paths(db)
    if not norm or not taxonomy:
        return None
    context = {
        "store_name": (receipt.shop_name or "").strip() or receipt.store_code,
        "store_code": receipt.store_code,
        "clarification": clarification.strip(),
    }
    if item.merchant_name:
        context["merchant_name"] = item.merchant_name
    if item.category_raw:
        context["store_category"] = item.category_raw
    guesses = await classify_hierarchical(db, [norm], taxonomy, {norm: context})
    guess = guesses.get(norm)
    if not guess or float(guess.confidence) < _MIN_CONFIDENCE:
        return None
    category_id = category_id_for_path(db, guess.category_path)
    if category_id is None:
        return None
    item.category_id = category_id
    db.commit()
    return category_id


def apply_guesses(
    db: Session,
    guesses: "dict[str, ai.CategoryGuess | None]",
    *,
    backend: str = "manual",
    include_other: bool = False,
) -> dict:
    """Cache one batch of category guesses in `product_category`, apply them to
    matching receipt items, and log a summary. Shared by the live pass
    (`run_classification_batch`) and the manual "bring your own AI" flow
    (`api/categories.py`). Never overrides a sticky `source="user"` row —
    except one that itself points at the catch-all "Інше" while `include_other`
    is set: being filed in the catch-all, even by hand, is what this whole pass
    is meant to fix, so it doesn't get the usual sticky protection here.
    Caller need not commit — this does."""
    now = dt.datetime.now(dt.timezone.utc)
    other_id = other_category_id(db) if include_other else None
    names = list(guesses)
    mapped = unknown_path = low_conf = no_guess = skipped_user = 0
    for norm in names:
        guess = guesses.get(norm)
        path = (guess.category_path if guess else "") or ""
        confidence = float(guess.confidence) if guess else 0.0
        cat_id = category_id_for_path(db, path) if path and confidence >= _MIN_CONFIDENCE else None
        if cat_id is not None:
            mapped += 1
        elif not path and not guess:
            no_guess += 1
        elif not path:
            pass  # deliberately "no category"
        elif confidence < _MIN_CONFIDENCE:
            low_conf += 1
        else:
            unknown_path += 1  # a path that isn't in the tree

        existing = db.get(ProductCategory, norm)
        sticky = existing is not None and existing.source == ProductCategorySource.user
        catch_all_pick = other_id is not None and existing is not None and existing.category_id == other_id
        if sticky and not catch_all_pick:
            skipped_user += 1
            continue
        if existing is None:
            db.add(ProductCategory(normalized_name=norm, category_id=cat_id,
                                   source=ProductCategorySource.ai, confidence=confidence))
        else:
            existing.category_id = cat_id
            existing.source = ProductCategorySource.ai
            existing.confidence = confidence
            existing.updated_at = now
    db.flush()
    items_tagged = _apply_cached_rows(db, names, include_other=include_other)
    db.commit()

    parts = [f"{mapped} розкладено"]
    if unknown_path:
        parts.append(f"{unknown_path} невідомий шлях")
    if low_conf:
        parts.append(f"{low_conf} низька впевненість")
    if no_guess:
        parts.append(f"{no_guess} без відповіді")
    if skipped_user:
        parts.append(f"{skipped_user} ручних збережено")
    ai.log_event(
        db, task="classify", backend=backend,
        note=f"{len(names)} назв: " + ", ".join(parts) + f"; позицій оновлено {items_tagged}",
        ok=mapped > 0 or (unknown_path == 0 and low_conf == 0 and no_guess == 0),
    )
    return {"names": len(names), "mapped": mapped, "unknown_path": unknown_path,
            "low_confidence": low_conf, "no_guess": no_guess,
            "skipped_manual": skipped_user, "items_tagged": items_tagged}


_LOOSE_ENTRY_RE = re.compile(
    r'\{\s*"category_path"\s*:\s*"([^"]*)"\s*,\s*"confidence"\s*:\s*([-0-9.eE]+)\s*\}'
)


def _key_to_norm(key: str, names: "list[str] | None") -> str:
    """Map one answer key to a normalised product name. When the manual prompt
    numbered the products (`names` given), an integer key is looked up in that
    list; otherwise the key is treated as the product name itself."""
    key = str(key).strip()
    if names and key.isdigit():
        idx = int(key) - 1
        if 0 <= idx < len(names):
            return normalize_name(names[idx]) or names[idx].strip().lower()
    return normalize_name(key) or key.lower()


def _loose_parse(text: str, names: "list[str] | None" = None) -> "dict[str, ai.CategoryGuess]":
    """Best-effort recovery when the pasted answer isn't valid JSON — chatbots
    sometimes leave an unescaped `"` inside a key. Anchor on the
    `{"category_path": …, "confidence": …}` value objects and take everything
    since the previous one as the key."""
    out: dict[str, ai.CategoryGuess] = {}
    prev_end = 0
    for m in _LOOSE_ENTRY_RE.finditer(text):
        segment = text[prev_end:m.start()]
        prev_end = m.end()
        key_part = segment.rsplit(":", 1)[0].strip().lstrip("{,").strip()
        if len(key_part) >= 2 and key_part[0] == '"' and key_part[-1] == '"':
            key_part = key_part[1:-1]
        norm = _key_to_norm(key_part, names)
        if not norm:
            continue
        try:
            conf = float(m.group(2))
        except ValueError:
            conf = 1.0
        out[norm] = ai.CategoryGuess(category_path=m.group(1).strip(), confidence=conf)
    return out


def parse_classification_result(
    text: str, names: "list[str] | None" = None
) -> "dict[str, ai.CategoryGuess]":
    """Parse whatever the user pasted back from a chatbot into
    {normalized_name: CategoryGuess}. Tolerates ```json fences, a bare string
    value (path only), and `path`/`category`/`category_path` key spellings.
    Falls back to a lenient regex scan when the text isn't strict JSON.

    `names` is the ordered product list the manual prompt sent — when given,
    integer answer keys are resolved through it (the prompt numbers products so
    the reply stays valid JSON regardless of quotes in a name)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:].strip() if text.lower().startswith("json") else text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        recovered = _loose_parse(text, names)
        if recovered:
            return recovered
        raise
    if not isinstance(data, dict):
        raise ValueError("очікується JSON-обʼєкт {товар: {category_path, confidence}}")
    out: dict[str, ai.CategoryGuess] = {}
    for key, val in data.items():
        norm = _key_to_norm(key, names)
        if isinstance(val, str):
            out[norm] = ai.CategoryGuess(category_path=val.strip(), confidence=1.0)
            continue
        if not isinstance(val, dict):
            continue
        path = val.get("category_path") or val.get("path") or val.get("category") or ""
        try:
            conf = float(val.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        out[norm] = ai.CategoryGuess(category_path=str(path).strip(), confidence=conf)
    return out


def _reapply_all_known(db: Session) -> int:
    """Re-stamp category_id on every currently-uncategorised item from whatever
    cache rows still hold a category (used after a forced reset to bring the
    sticky `source="user"` choices straight back)."""
    resolved = {
        r.normalized_name: r.category_id
        for r in db.execute(
            select(ProductCategory).where(ProductCategory.category_id.is_not(None))
        ).scalars()
    }
    if not resolved:
        return 0
    applied = 0
    for item in db.execute(
        select(ReceiptItem).where(ReceiptItem.category_id.is_(None))
    ).scalars():
        cat_id = resolved.get(normalize_name(item.name))
        if cat_id is not None:
            item.category_id = cat_id
            applied += 1
    return applied


def reset_ai_classifications(db: Session) -> dict:
    """Forced re-split: drop every AI-made alias and clear every item category,
    then bring the sticky manual (`source="user"`) choices back. After this the
    whole history is queued for re-classification again. Caller need not commit —
    this does."""
    aliases_cleared = db.execute(
        delete(ProductCategory).where(ProductCategory.source == ProductCategorySource.ai)
    ).rowcount or 0
    db.execute(update(ReceiptItem).values(category_id=None))
    db.flush()
    reapplied = _reapply_all_known(db)
    db.commit()
    return {"aliases_cleared": aliases_cleared, "items_reapplied": reapplied}


def reset_all_classifications(db: Session) -> dict:
    """Wipe *everything* category-related — every `product_category` alias
    (AI **and** manual `source="user"`) and every item `category_id`. Unlike
    `reset_ai_classifications` nothing is brought back; the whole history lands
    back in the "Без категорії" queue. No model calls. Caller need not commit —
    this does."""
    aliases_cleared = db.execute(delete(ProductCategory)).rowcount or 0
    items_cleared = (
        db.execute(
            update(ReceiptItem)
            .where(ReceiptItem.category_id.is_not(None))
            .values(category_id=None)
        ).rowcount
        or 0
    )
    db.commit()
    return {"aliases_cleared": aliases_cleared, "items_cleared": items_cleared}


async def backfill_all(
    db: Session, batch_size: int = 50, max_batches: int = 100_000,
    *, force: bool = False, include_other: bool = False,
) -> dict:
    """Classify everything that still needs it — the one-off history pass
    (ROADMAP.md "Разовий бекфіл усієї історії").

    Standard mode only touches the "Без категорії" queue (items with no category
    and no alias row yet); `include_other=True` folds the catch-all "Інше" into
    that same queue. `force=True` first wipes all AI-made classifications
    (keeping manual ones) so the entire history is re-split from scratch — for
    when the tree or the model changed a lot.
    """
    reset: dict | None = None
    if force:
        reset = reset_ai_classifications(db)

    total = 0
    batches = 0
    while batches < max_batches:
        n = await run_classification_batch(db, batch_size, include_other=include_other)
        if n == 0:
            break
        total += n
        batches += 1
    return {"names_classified": total, "batches": batches, "reset": reset}
