"""Small text helpers shared across the categorisation pipeline (ROADMAP.md
Крок 1) and, later, the shopping list (Крок 2).

`normalize_name` collapses the many spellings a store uses for the same product
("Молоко Яготинське 2,6% 900г" / "МОЛОКО ЯГОТИНСЬКЕ 2.6%  900 Г") down to one
key so it's classified — and matched — at most once.
"""
from __future__ import annotations

import re

# Trailing quantity / packaging tokens: "0,9 л", "1 кг", "500г", "12 шт", "2x1л".
_QTY_TAIL_RE = re.compile(
    r"[\s,;xх*/]*\d+(?:[.,]\d+)?\s*"
    r"(?:кг|г|мг|л|мл|шт|уп|пак|пл|pcs|kg|g|mg|l|ml)\.?\s*$",
    re.IGNORECASE,
)
# Leading store article / PLU codes: "1234567 ", "art.1234 ".
_LEAD_CODE_RE = re.compile(r"^\s*(?:арт\.?|art\.?|код)?\s*\d{4,}\s+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
# Percentages ("2,6%") carry no categorisation signal and vary per SKU.
_PERCENT_RE = re.compile(r"\s*\d+(?:[.,]\d+)?\s*%")


def normalize_name(raw: str | None) -> str:
    text = (raw or "").strip().casefold()
    if not text:
        return ""
    text = _LEAD_CODE_RE.sub("", text)
    text = _PERCENT_RE.sub("", text)
    # A name can end in several tail tokens ("... 900 г уп"); strip repeatedly.
    while True:
        stripped = _QTY_TAIL_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = _WS_RE.sub(" ", text).strip(" -.,")
    return text


# Minimal Ukrainian/Russian → latin map for category slugs. Not a full
# transliteration standard — just enough to produce stable, readable path
# segments ("produkty/molochne").
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ъ": "", "ы": "y", "э": "e", "ю": "iu",
    "я": "ia",
}


# Receipt hashtags — keep them readable (Cyrillic stays), just make them a
# single safe token: "#Поездка Львов, лето-2026" -> "поездка_львов_лето_2026".
_TAG_SEP_RE = re.compile(r"[\s\-./\\,;:|]+")
_TAG_DROP_RE = re.compile(r"[^\w]", re.UNICODE)   # \w keeps letters/digits/_ incl. Cyrillic
_TAG_US_RE = re.compile(r"_+")


def normalize_tag(raw: str | None) -> str:
    s = (raw or "").strip().lstrip("#").strip().casefold()
    s = _TAG_SEP_RE.sub("_", s)
    s = _TAG_DROP_RE.sub("", s)
    s = _TAG_US_RE.sub("_", s).strip("_")
    return s[:64]


def normalize_tags(raw: "list[str] | None") -> list[str]:
    """Normalise, drop empties, de-duplicate while keeping first-seen order."""
    seen: dict[str, None] = {}
    for item in raw or []:
        t = normalize_tag(item)
        if t and t not in seen:
            seen[t] = None
    return list(seen)


def slugify(name: str) -> str:
    out = []
    for ch in name.strip().casefold():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug or "cat"
