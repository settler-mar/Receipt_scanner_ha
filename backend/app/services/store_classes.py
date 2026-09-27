"""Where a purchase is made: the user-facing store classes (SHOPPING_LIST_WORKPLAN.md,
приоритет 3 — "вынести пользовательские классы магазинов в backend").

One dictionary for everything that needs to say "this is a grocery / a DIY shop
/ a pharmacy": the shopping list groups rows by it, the store catalogue labels
chains with it, and Telegram/MCP can reuse it instead of re-inventing local
rules. Two resolvers:

  * `class_for_store`      — the chain the user explicitly picked for a row;
  * `class_for_category`   — the fallback for rows with no store, derived from
                             the *top level* of the product taxonomy.

Nothing here is per-user configuration: it is reference data, like `seed.py`.
"""
from __future__ import annotations

from app.models.models import Store

# Display order == the order groups appear in the UI.
CLASSES: tuple[tuple[str, str], ...] = (
    ("grocery", "Продуктовые"),
    ("pharmacy", "Аптека и косметика"),
    ("diy", "Строительные"),
    ("home", "Для дома"),
    ("electronics", "Техника"),
    ("clothing", "Одежда"),
    ("pet", "Зоотовары"),
    ("marketplace", "Маркетплейсы"),
    ("fuel", "АЗС и авто"),
    ("manual", "Ручное добавление"),
    ("other", "Другое"),
)
UNKNOWN = "unknown"
UNKNOWN_LABEL = "Без группы"

_LABELS = dict(CLASSES)

# Chains we know by name. Kept ahead of `network_type` because the seeded chain
# type is coarser than the class (Епіцентр is seeded "home" but shops as DIY).
_BY_CODE: dict[str, str] = {
    "wog": "fuel", "ukrnafta": "fuel", "bars": "fuel", "brsm": "fuel",
    "upg": "fuel", "klo": "fuel",
    "epicentrk": "diy", "dnipro-m": "diy",
    "foxtrot": "electronics", "vencon": "electronics", "brain": "electronics",
    "prom": "marketplace", "rozetka": "marketplace", "aliexpress": "marketplace",
    "atb": "grocery", "fozzy": "grocery", "fora": "grocery", "silpo": "grocery",
    "metro": "grocery",
    "eva": "pharmacy", "tabletki": "pharmacy",
    "scanner": "manual",
    # A bank feed, not a shop — never a place a shopping-list row was bought at.
    "monobank": "other",
}

_BY_NETWORK: dict[str, str] = {
    "supermarket": "grocery", "azs": "fuel", "tech": "electronics",
    "pharmacy": "pharmacy", "beauty": "pharmacy", "home": "home",
}

# Top-level taxonomy path -> class, for rows the user has not pinned to a shop.
_BY_CATEGORY_ROOT: dict[str, str] = {
    "produkty": "grocery",
    "napoi": "grocery",
    "solodoshchi-ta-sneky": "grocery",
    "dity": "grocery",
    "zdorovia-ta-apteka": "pharmacy",
    "hihiiena-ta-kosmetyka": "pharmacy",
    "budivnytstvo-ta-remont": "diy",
    "inzhenerni-systemy-budynku": "diy",
    "sad-ta-dilianka": "diy",
    "dim-ta-kukhnia": "home",
    "pobutova-khimiia-ta-hospodarski-tovary": "home",
    "tekhnika-ta-elektronika": "electronics",
    "rozumnyi-dim-ta-bezpeka": "electronics",
    "odiah-ta-vzuttia": "clothing",
    "avto": "fuel",
    "dozvillia": "other",
    "osvita": "other",
    "podorozhi": "other",
    "posluhy-ta-platezhi": "other",
    "inshe": "other",
}


def label(code: str | None) -> str:
    return _LABELS.get(code or "", UNKNOWN_LABEL)


def as_options() -> list[dict[str, str]]:
    """The dictionary itself, for clients that render group headers."""
    return [{"code": code, "name": name} for code, name in CLASSES]


def class_for_store(store: Store | None) -> str | None:
    if store is None:
        return None
    known = _BY_CODE.get(store.code)
    if known:
        return known
    return _BY_NETWORK.get(store.network_type or "", "other")


def class_for_category(path: str | None) -> str | None:
    """Class implied by a category path — only its top level matters."""
    if not path:
        return None
    return _BY_CATEGORY_ROOT.get(path.split("/", 1)[0], "other")
