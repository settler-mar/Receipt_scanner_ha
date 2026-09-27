"""MCC (merchant category code) -> Category tree node, for bank transactions
that never produce a receipt (see services/monobank.py).

A small static table is enough here, unlike the free-text product names
`ProductCategory` caches (models.py) — MCC is a fixed, ISO-8583-standardised
code list, not something new values keep showing up in. Names below are exact
leaf/root labels from `seed.CATEGORY_TREE`; a name that doesn't exist yet in a
given database (someone edited the tree) just resolves to no category.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Category

# https://en.wikipedia.org/wiki/Merchant_category_code — codes actually seen
# on a personal Monobank statement, mapped to a category name from
# app/seed.py's CATEGORY_TREE.
MCC_TO_CATEGORY_NAME: dict[int, str] = {
    4900: "Інші комунальні",
    4899: "Підписки та онлайн-сервіси",
    4814: "Мобільний зв'язок",
    4812: "Мобільний зв'язок",
    4816: "Інтернет",
    5411: "Продукти",
    5422: "М'ясо та птиця",
    5441: "Солодощі та снеки",
    5462: "Хліб та випічка",
    5499: "Продукти",
    5811: "Ресторани та кафе",
    5812: "Ресторани та кафе",
    5813: "Ресторани та кафе",
    5814: "Фастфуд",
    5912: "Здоров'я та аптека",
    5921: "Алкоголь",
    4111: "Громадський транспорт",
    4121: "Таксі",
    7523: "Паркування",
    5541: "Бензин",
    5542: "Бензин",
    5533: "Запчастини та розхідники",
    5941: "Спорт та активний відпочинок",
    5945: "Іграшки",
    5651: "Одяг та взуття",
    5661: "Взуття",
    5732: "Техніка та електроніка",
    5734: "Комп'ютери та ноутбуки",
    5992: "Квіти та подарунки",
    5942: "Книги та преса",
    7832: "Кіно, театр та концерти",
    4722: "Туристичні послуги",
    7011: "Проживання",
    4511: "Квитки",
    8011: "Лікарі та консультації",
    8021: "Стоматологія",
    8062: "Медичні послуги",
    8099: "Здоров'я та аптека",
    8211: "Шкільні товари",
    8220: "Освітні послуги",
    9402: "Доставка та пошта",
}

_cache: dict[int, str | None] = {}


def resolve(db: Session, mcc: int | None) -> Category | None:
    """Best-effort category for an MCC code. None when unmapped or unknown."""
    if mcc is None:
        return None
    name = MCC_TO_CATEGORY_NAME.get(mcc)
    if name is None:
        return None
    if mcc not in _cache:
        row = db.execute(select(Category).where(Category.name == name)).scalars().first()
        _cache[mcc] = row.id if row else None
    category_id = _cache[mcc]
    return db.get(Category, category_id) if category_id else None
