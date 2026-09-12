"""The tax-cabinet URL a fiscal QR carries: parsing, validation, identity.

Split out of the decoding side on purpose — everything here is pure string
work, has no opencv/zxing dependency, and is what the rest of the app imports
when it already has a URL in hand (fiscal_queue, manual_dedup, telegram).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from urllib.parse import parse_qs, urlparse

from app.core.time import store_local_to_utc

_TAX_HOST_RE = re.compile(r"(^|\.)tax\.gov\.ua$", re.IGNORECASE)


@dataclasses.dataclass
class FiscalQr:
    raw_url: str
    fiscal_number: str | None = None      # `fn` — the registrar's fiscal number
    document_id: str | None = None        # `id` — this receipt's fiscal doc number
    issued_at: dt.datetime | None = None  # aware UTC
    total_amount: float | None = None

    @property
    def dedup_key(self) -> str | None:
        """Stable id for this physical receipt, for the `external_id` slot so a
        re-scan of the same paper doesn't create a duplicate."""
        if self.fiscal_number and self.document_id:
            return f"fiscal:{self.fiscal_number}:{self.document_id}"
        return None


def fiscal_identity(url: str | None) -> str | None:
    """The `fiscal:<registrar>:<document>` key behind any tax-cabinet URL.

    Two records naming the same ДПС document are the same purchase, whatever
    else about them was read differently — so this is the strongest duplicate
    signal there is, and it can be compared across receipts, scans and links.
    """
    if not url:
        return None
    parsed = parse_fiscal_url(str(url))
    return parsed.dedup_key if parsed else None


def _parse_dt(date_s: str | None, time_s: str | None) -> dt.datetime | None:
    if not date_s:
        return None
    date_s = date_s.strip()
    time_s = (time_s or "0000").strip().replace(":", "")
    for date_fmt in ("%Y%m%d", "%d.%m.%Y", "%d%m%Y"):
        try:
            d = dt.datetime.strptime(date_s, date_fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    time_s = (time_s + "000000")[:6]
    try:
        t = dt.datetime.strptime(time_s, "%H%M%S").time()
    except ValueError:
        t = dt.time()
    return store_local_to_utc(dt.datetime.combine(d, t))


def _parse_amount(value: str | None) -> float | None:
    if not value:
        return None
    value = value.replace(",", ".").replace(" ", "")
    try:
        return round(float(value), 2)
    except ValueError:
        return None


def parse_fiscal_url(url: str) -> FiscalQr | None:
    """Parse a decoded QR payload. Returns None if it isn't a tax-cabinet URL.

    This is the only gate a payload passes to count as fiscal, however hard the
    preprocessing that produced it had to work: http(s) scheme, a tax.gov.ua
    host, and a query we can read. Anything else is a shop's own QR or an advert
    and must never end the search for the fiscal one.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not _TAX_HOST_RE.search(parsed.hostname or ""):
        return None
    q = {k.lower(): v[0] for k, v in parse_qs(parsed.query).items() if v}
    return FiscalQr(
        raw_url=url.strip(),
        fiscal_number=q.get("fn"),
        document_id=q.get("id") or q.get("nfd"),
        issued_at=_parse_dt(q.get("date"), q.get("time")),
        total_amount=_parse_amount(q.get("sum") or q.get("sm") or q.get("s")),
    )


def is_fiscal(payload: str) -> bool:
    return parse_fiscal_url(payload) is not None
