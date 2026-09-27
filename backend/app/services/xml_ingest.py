"""The one place a ДПС XML enters the system, whoever hands it over.

A receipt XML can arrive from four screens (a queue row, a scan row, the free
import box, a pasted `chkAllWeb` link) and in three shapes (`<CHECK>`, `<RQ>`,
`<Chk>`). Before this module each screen did its own half of the work, so the
same file could mean "imported" in one place and nothing at all in another, and
a file belonging to a different purchase was accepted onto whatever row the user
happened to press.

`ingest()` answers the whole question once: what document is this, is it already
in the database, does it belong to the row it was dropped on, and — when it is a
receipt nobody has seen before — it opens the inbox row for it and runs the same
attach/queue steps a scanned photo would have gone through.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.base import NormalizedReceipt
from app.core.time import KYIV, as_utc
from app.models.models import Receipt
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus, ScanJob, ScanStatus
from app.scanner.cabinet_check import parse_cabinet_check_xml
from app.scanner.fiscal_check import parse_check_xml
from app.scanner.fiscal_qr import fiscal_identity, parse_fiscal_url
from app.services import fiscal_queue, scan_jobs
from app.services.manual_dedup import find_duplicate, find_receipt_by_fiscal

#: How far two timestamps of the same purchase may sit apart before the pair is
#: read as two different days. A misread hour shifts a receipt by a couple of
#: hours, never by a day — so anything past this is a different receipt.
_DAY_TOLERANCE = dt.timedelta(hours=26)

#: How close in time two records of one purchase are, once their money agrees
#: to the kopeck. The same window the ДПС queue uses to recognise its own
#: receipt among those that arrived by other routes.
_SAME_PURCHASE_WINDOW = dt.timedelta(minutes=5)


class XmlIngestError(ValueError):
    """The upload is not a ДПС document we can read."""


class XmlTargetMismatch(XmlIngestError):
    """The document is readable, but it is not the one this row is waiting for."""


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #
def decode_xml(data: bytes) -> str:
    """Text of an uploaded XML file. ДПС serves both UTF-8 and windows-1251."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1251")
        except UnicodeDecodeError as exc:
            raise XmlIngestError("Невідоме кодування XML-файлу") from exc


def _viewer_url(url: str, root: ET.Element, check=None) -> str:
    query = parse_qs(urlparse(url).query) if url else {}
    fn = (query.get("fn") or [root.findtext("fn") or getattr(check, "fiscal_number", "") or ""])[0]
    params = {key: (query.get(key) or [""])[0] for key in ("date", "id", "sm")}
    if check:
        params["id"] = params["id"] or check.document_id or ""
        params["sm"] = params["sm"] or (f"{check.total:.2f}" if check.total else "")
        if not params["date"] and check.issued_at:
            local = check.issued_at.astimezone(KYIV)
            params["date"] = local.strftime("%Y%m%d")
            params["time"] = local.strftime("%H%M%S")
    params["fn"] = fn
    return "https://cabinet.tax.gov.ua/cashregs/check?" + urlencode(
        {k: v for k, v in params.items() if v})


def parse_dps_xml(body: str, source_url: str = "") -> tuple[NormalizedReceipt, str]:
    """Any of the three ДПС shapes → (receipt, tax-cabinet viewer URL or "").

    An empty URL means the document names no fiscal check number, so there is
    nothing to queue against — it is already the answer the queue exists to get.
    """
    start = body.find("<Chk")
    if start < 0 and "<CHECK" in body:
        # The cabinet's own per-receipt download: already decoded, and with no
        # fiscal document number.
        receipt = parse_cabinet_check_xml(body)
        if receipt is None:
            raise XmlIngestError("Файл схожий на <CHECK>, але прочитати його не вдалося")
        return receipt, ""
    if start < 0:
        # "Save page" on the DPS result downloads the decoded fiscal RQ
        # document directly, rather than the outer Chk/base64 envelope.
        rq_start = body.find("<RQ")
        declaration = body.find("<?xml")
        if rq_start < 0:
            raise XmlIngestError("У файлі немає фіскального XML <RQ> або відповіді <Chk>")
        raw = body[declaration if 0 <= declaration < rq_start else rq_start:]
        # parse_check_xml already resolves ПН/ЄДРПОУ to the retail brand.
        check = parse_check_xml(raw)
        url = _viewer_url(source_url, ET.Element("RQ"), check)
        check.raw_url = url
        return check.to_normalized_receipt(), url
    root = ET.fromstring(body[start:])
    xml_b64 = (root.findtext("checkXml") or "").strip()
    if not xml_b64:
        raise XmlIngestError("ДПС не повернула поле checkXml")
    plain_b64 = (root.findtext("check") or "").strip()
    plain = None
    if plain_b64:
        plain_bytes = base64.b64decode(plain_b64)
        try:
            plain = plain_bytes.decode("utf-8")
        except UnicodeDecodeError:
            plain = plain_bytes.decode("cp1251", "replace")
    check = parse_check_xml(base64.b64decode(xml_b64), plaintext=plain)
    url = _viewer_url(source_url, root, check)
    check.raw_url = url
    return check.to_normalized_receipt(), url


# --------------------------------------------------------------------------- #
# "Is this the receipt that row is waiting for?"
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _Identity:
    """What is known about which physical receipt a thing refers to."""

    key: str | None = None           # fiscal:<registrar>:<document>
    registrar: str | None = None     # the till's fiscal number (`fn`)
    issued_at: dt.datetime | None = None
    total: float | None = None
    label: str = ""                  # how to name it to the user


def _money(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _identity_of_xml(receipt: NormalizedReceipt, viewer_url: str) -> _Identity:
    fiscal = parse_fiscal_url(viewer_url) if viewer_url else None
    registrar = (fiscal.fiscal_number if fiscal else None) or (receipt.raw or {}).get("fiscal_number")
    return _Identity(
        key=fiscal.dedup_key if fiscal else None,
        registrar=str(registrar) if registrar else None,
        issued_at=as_utc(receipt.purchased_at),
        total=_money(receipt.total_amount),
        label="завантажений XML",
    )


def _identity_of_receipt(receipt: Receipt | None) -> _Identity:
    if receipt is None:
        return _Identity()
    external = str(receipt.external_id or "")
    key = ((external if external.startswith("fiscal:") else None)
           or fiscal_identity(receipt.receipt_url)
           or fiscal_identity((receipt.raw or {}).get("fiscal_url")))
    registrar = key.split(":")[1] if key else (receipt.raw or {}).get("fiscal_number")
    return _Identity(key=key, registrar=str(registrar) if registrar else None,
                     issued_at=as_utc(receipt.purchased_at),
                     total=_money(receipt.total_amount))


def _identity_of_scan(db: Session, job: ScanJob) -> _Identity:
    """What the scan row knows about its own receipt — QR first, then the
    receipt it already filed, then the draft awaiting review."""
    raw = job.raw_ocr or {}
    fiscal = parse_fiscal_url(str(raw.get("fiscal_url"))) if raw.get("fiscal_url") else None
    identity = _Identity(label="цей рядок сканування")
    if fiscal is not None:
        identity.key = fiscal.dedup_key
        identity.registrar = fiscal.fiscal_number
        identity.issued_at = fiscal.issued_at
        identity.total = _money(fiscal.total_amount)
    if job.receipt_id:
        filed = _identity_of_receipt(db.get(Receipt, job.receipt_id))
        identity.key = identity.key or filed.key
        identity.registrar = identity.registrar or filed.registrar
        identity.issued_at = identity.issued_at or filed.issued_at
        identity.total = identity.total if identity.total is not None else filed.total
    draft = raw.get("draft") or {}
    if identity.total is None and draft.get("total_amount") is not None:
        identity.total = _money(draft.get("total_amount"))
    if identity.issued_at is None and draft.get("purchased_at"):
        try:
            identity.issued_at = as_utc(dt.datetime.fromisoformat(str(draft["purchased_at"])))
        except ValueError:
            pass
    return identity


def _identity_of_queue(job: FiscalQueueJob) -> _Identity:
    fiscal = parse_fiscal_url(job.url)
    if fiscal is None:
        return _Identity(label="цей рядок черги ДПС")
    return _Identity(key=fiscal.dedup_key, registrar=fiscal.fiscal_number,
                     issued_at=fiscal.issued_at, total=_money(fiscal.total_amount),
                     label="цей рядок черги ДПС")


def _same_purchase(left: _Identity, right: _Identity) -> bool:
    """Two identities describing one purchase, without a document number.

    The ДПС document id is the certain answer, but it is not always there to
    compare: a QR that decoded only partly, an `<RQ>` that names no check
    number. What is left is money and the minute — and a second purchase for the
    very same kopeck within minutes of the first is not something that happens.
    """
    if left.total is None or right.total is None:
        return False
    if not (left.issued_at and right.issued_at):
        return False
    return (abs(left.total - right.total) <= 0.01
            and abs(left.issued_at - right.issued_at) <= _SAME_PURCHASE_WINDOW)


def _when(moment: dt.datetime | None) -> str:
    return as_utc(moment).astimezone(KYIV).strftime("%d.%m.%Y %H:%M") if moment else "?"


def verify_target(incoming: _Identity, target: _Identity) -> None:
    """Refuse an XML that plainly describes a different purchase.

    Only hard contradictions count. A wrong file is the common accident (the
    cabinet names every download after the document), but the whole reason to
    import an XML by hand is that the row's own reading is poor — so a
    disagreement that one misread receipt could have produced on its own is
    never enough to reject on.
    """
    # Money to the kopeck at the same minute settles it before any id is
    # compared: `check?id=…` is not always the fiscal document number — several
    # ПРРО put an opaque document token there ("BZ0nkpp_COs"), which the
    # document's own number (5641) can never be equal to. Judging those two by
    # their ids alone refused the right file for the right row.
    if _same_purchase(incoming, target):
        return
    if incoming.key and target.key and incoming.key != target.key:
        raise XmlTargetMismatch(
            f"Цей XML — чек {incoming.key.removeprefix('fiscal:')}, "
            f"а {target.label or 'обраний рядок'} чекає на {target.key.removeprefix('fiscal:')}. "
            "Завантажте файл у загальний імпорт XML — система сама знайде потрібний рядок.")
    if (incoming.registrar and target.registrar
            and incoming.registrar != target.registrar):
        raise XmlTargetMismatch(
            f"Цей XML з РРО {incoming.registrar}, а {target.label or 'обраний рядок'} — "
            f"з РРО {target.registrar}. Це різні чеки.")
    # Nothing identifies the document itself: fall back to "different day *and*
    # different money", which no misread of one receipt produces at once.
    if (incoming.total is not None and target.total is not None
            and incoming.issued_at and target.issued_at
            and abs(incoming.total - target.total) > 0.01
            and abs(incoming.issued_at - target.issued_at) > _DAY_TOLERANCE):
        raise XmlTargetMismatch(
            f"Цей XML — на {incoming.total:.2f} грн від {_when(incoming.issued_at)}, "
            f"а {target.label or 'обраний рядок'} — на {target.total:.2f} грн "
            f"від {_when(target.issued_at)}. Схоже, це файл іншого чека.")


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class XmlIngestOutcome:
    """What the upload turned into — the same answer for every caller."""

    receipt_id: str | None = None
    scan_job_id: str | None = None
    queue_job_id: str | None = None
    queue_status: str = FiscalQueueStatus.imported.value
    # "created" — a receipt nobody had; "merged" — the document met a receipt
    # already on file and was folded into it.
    outcome: str = "created"
    duplicate: bool = False
    scan_job_created: bool = False
    fiscal_key: str | None = None
    viewer_url: str = ""
    shop_name: str | None = None
    purchased_at: dt.datetime | None = None
    total_amount: float | None = None
    message: str = ""


def _scan_job_for(db: Session, incoming: _Identity, receipt_id: str | None) -> ScanJob | None:
    """An inbox row that is already about this receipt, if one exists.

    A photo scanned earlier and this XML are two attempts at one purchase, so
    the import belongs on that row rather than beside it as a second entry —
    which is what the user sees, two rows for one receipt, when this misses.
    """
    rows = db.execute(select(ScanJob).order_by(ScanJob.created_at)).scalars().all()
    # The row that already filed this very receipt.
    for job in rows:
        if receipt_id and job.receipt_id == receipt_id:
            return job
    # The row whose QR names the same ДПС document.
    if incoming.key:
        for job in rows:
            if fiscal_identity((job.raw_ocr or {}).get("fiscal_url")) == incoming.key:
                return job
    # A row still waiting for its receipt that describes the same purchase.
    # "Waiting" is asked of the receipts table, not of the column: deleting a
    # receipt by hand leaves the id dangling, and a row pointing at a receipt
    # that no longer exists is exactly one that needs this file.
    for job in rows:
        if job.archived_at or scan_jobs.live_receipt_id(db, job):
            continue
        if _same_purchase(incoming, _identity_of_scan(db, job)):
            return job
    return None


def _open_inbox_row(db: Session, *, viewer_url: str, filename: str | None,
                    size_kb: int | None) -> ScanJob:
    """Create the inbox task a brand-new XML receipt deserves.

    Everything else in the app reads the scan journal to answer "where did this
    receipt come from" — an import that created no row was invisible there.
    """
    job = ScanJob(source_filename=filename or "Імпорт XML ДПС", status=ScanStatus.pending,
                  raw_ocr={"fiscal_url": viewer_url or None,
                           "notes": ["Чек створено з XML-файлу ДПС, без фото"]})
    db.add(job)
    db.commit()
    db.refresh(job)
    detail = filename or "XML ДПС"
    if size_kb:
        detail = f"{detail} · {size_kb} КБ"
    scan_jobs.record_event(db, job, "upload", detail=detail)
    return job


def ingest(db: Session, body: str, *, source_url: str = "", filename: str | None = None,
           size_bytes: int = 0, scan_job_id: str | None = None,
           fiscal_job_id: str | None = None) -> XmlIngestOutcome:
    """Take an arbitrary ДПС XML and put it where it belongs.

    `scan_job_id` / `fiscal_job_id` say which row the user dropped the file on;
    both are checked against the document and refused when they disagree with
    it. With neither, the document is matched to whatever the database already
    holds, and a receipt that is genuinely new gets its own inbox row.
    """
    from app.api.inbox import _persist_receipt, _scanner_account

    scan_job = db.get(ScanJob, scan_job_id) if scan_job_id else None
    queue_job = db.get(FiscalQueueJob, fiscal_job_id) if fiscal_job_id else None
    if scan_job_id and scan_job is None:
        raise XmlIngestError("Сканування не знайдено")
    if fiscal_job_id and queue_job is None:
        raise XmlIngestError("Рядок черги ДПС не знайдено")

    try:
        receipt, viewer_url = parse_dps_xml(body, source_url)
    except XmlIngestError:
        raise
    except Exception as exc:  # noqa: BLE001 — any parser fault is a bad file
        raise XmlIngestError(str(exc)) from exc

    incoming = _identity_of_xml(receipt, viewer_url)
    if scan_job is not None:
        verify_target(incoming, _identity_of_scan(db, scan_job))
    if queue_job is not None:
        verify_target(incoming, _identity_of_queue(queue_job))

    fiscal = parse_fiscal_url(viewer_url) if viewer_url else None
    # Decided before persisting: afterwards a merge is indistinguishable from an
    # insert, and the user needs to be told which of the two happened.
    known = find_receipt_by_fiscal(db, fiscal) if fiscal else None
    if known is None:
        known = find_duplicate(db, receipt)

    account = _scanner_account(db)
    saved = _persist_receipt(db, account, receipt)
    db.commit()

    result = XmlIngestOutcome(
        receipt_id=saved.id, viewer_url=viewer_url, fiscal_key=incoming.key,
        duplicate=known is not None,
        outcome="merged" if known is not None else "created",
        shop_name=saved.shop_name, purchased_at=as_utc(saved.purchased_at),
        total_amount=_money(saved.total_amount),
    )

    # The inbox row: the one the file was dropped on, the one already about this
    # receipt, or — for a receipt nobody has seen — a new one.
    if scan_job is None:
        scan_job = _scan_job_for(db, incoming, saved.id)
    if scan_job is None:
        scan_job = _open_inbox_row(db, viewer_url=viewer_url, filename=filename,
                                   size_kb=size_bytes // 1024 or None)
        result.scan_job_created = True
    method = (receipt.raw or {}).get("method") or "tax_cabinet"
    scan_jobs.attach_receipt(db, scan_job.id, saved.id, method=method)
    result.scan_job_id = scan_job.id

    # The ДПС queue: a document that names a check number either closes the row
    # that was waiting for it or records that this receipt never needed one.
    # The scanned QR's own link wins over the one rebuilt from the document —
    # when the two disagree it is because `id` there is an opaque token, and
    # queueing the rebuilt link would open a second row for the same receipt.
    queue_url = (str((scan_job.raw_ocr or {}).get("fiscal_url") or "")
                 if fiscal_identity((scan_job.raw_ocr or {}).get("fiscal_url"))
                 else "") or viewer_url
    result.viewer_url = queue_url
    if queue_url:
        try:
            queue_job = queue_job or fiscal_queue.enqueue(db, queue_url,
                                                          scan_job_id=scan_job.id)
        except ValueError:
            queue_job = None
        if queue_job is not None:
            queue_job.status = FiscalQueueStatus.imported
            queue_job.receipt_id = saved.id
            queue_job.error = None
            queue_job.scan_job_id = scan_job.id
            db.commit()
            result.queue_job_id = queue_job.id
            result.queue_status = queue_job.status.value

    result.message = _message(result)
    return result


def ingest_in_background(body: str, *, source_url: str = "", filename: str | None = None,
                         size_bytes: int = 0, scan_job_id: str | None = None,
                         fiscal_job_id: str | None = None) -> XmlIngestOutcome:
    """Run XML ingestion with a session owned by the worker thread.

    SQLAlchemy sessions are not thread-safe.  The HTTP request session must not
    be passed into ``asyncio.to_thread`` together with the expensive parser and
    persistence work, so the worker creates and closes its own session here.
    """
    from app.db import SessionLocal

    with SessionLocal() as db:
        return ingest(
            db, body, source_url=source_url, filename=filename,
            size_bytes=size_bytes, scan_job_id=scan_job_id,
            fiscal_job_id=fiscal_job_id,
        )


def _message(result: XmlIngestOutcome) -> str:
    """One line saying what the file was and where it went.

    Both halves matter: "чек уже був у базі" on its own leaves the user looking
    for a journal row that changed, with no way to tell which one did.
    """
    what = " ".join(part for part in (
        f"{result.total_amount:.2f} UAH" if result.total_amount is not None else "",
        _when(result.purchased_at),
    ) if part)
    where = ("для нього створено рядок у вхідних" if result.scan_job_created
             else "додано до наявного рядка сканування")
    if result.outcome == "created":
        return f"Новий чек збережено з XML ({what}) — {where}."
    return (f"Чек уже був у базі — дублікат не створено, дані з XML додано "
            f"до нього ({what}) — {where}.")
