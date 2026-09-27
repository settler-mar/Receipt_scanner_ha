from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
from app.scanner import tax_cabinet
from app.scanner.fiscal_qr import parse_fiscal_url
from app.services import fiscal_queue, scan_jobs, tax_cabinet_settings, xml_ingest
from app.services import fiscal_browser
from app.api.inbox import _persist_receipt, _scanner_account
from app.core.time import as_utc

router = APIRouter(prefix="/api/fiscal-queue", tags=["fiscal queue"])

# `scan_job_id` ties the import back to the inbox row it was started from, so
# that row can show the receipt instead of staying on "needs_review".
class AddIn(BaseModel):
    url: str
    scan_job_id: str | None = None
class ImportWebResponseIn(BaseModel):
    value: str
    scan_job_id: str | None = None
    # The ДПС queue row the file was dropped on, when it was dropped on one:
    # checked against the document so another receipt's XML can't land here.
    fiscal_job_id: str | None = None

def out(j: FiscalQueueJob):
    return {"id":j.id,"url":j.url,"source":j.source,"status":j.status.value,
            "attempts":j.attempts,"receipt_id":j.receipt_id,"scan_job_id":j.scan_job_id,"error":j.error,
            # Only a session that still exists in this process is worth showing:
            # a browser session does not survive a restart, and a stale id would
            # make the UI poll a screen that can never come back.
            "session_id":j.session_id if fiscal_browser.is_live(j.session_id,j.id) else None,"created_at":as_utc(j.created_at),"updated_at":as_utc(j.updated_at),
            "receipt_url":f"/receipts/{j.receipt_id}" if j.receipt_id else None}

@router.get("")
def list_jobs(open_only: bool = False, db: Session = Depends(get_db)):
    """`open_only` — только строки, которые ещё чего-то ждут: чек, полученный
    другим способом (фото/AI/вручную), очередь ДПС больше не держит."""
    if open_only:
        return [out(x) for x in fiscal_queue.open_jobs(db)]
    return [out(x) for x in db.execute(select(FiscalQueueJob).order_by(FiscalQueueJob.created_at.desc())).scalars()]

@router.post("")
def add(payload: AddIn, db: Session = Depends(get_db)):
    try: return out(fiscal_queue.enqueue(db, payload.url))
    except ValueError as e: raise HTTPException(400, str(e)) from e

async def _fetch_with_public_api(job: FiscalQueueJob, db: Session, *, require_token: bool = True):
    token=tax_cabinet_settings.get_token(db)
    if not token:
        if require_token:
            raise HTTPException(409,"Токен відкритої частини ДПС не налаштований")
        job.status=FiscalQueueStatus.captcha
        job.error="Токен відкритої частини ДПС не налаштований. Доступна ручна CAPTCHA."
        db.commit(); return out(job)
    qr=parse_fiscal_url(job.url)
    job.status=FiscalQueueStatus.processing; fiscal_queue.touch_attempt(db,job)
    receipt=await tax_cabinet.fetch_structured(qr,api_token=token)
    if receipt:
        saved=_persist_receipt(db,_scanner_account(db),receipt)
        job.status=FiscalQueueStatus.imported; job.receipt_id=saved.id
        if job.session_id:
            await fiscal_browser.close(job.session_id)
        job.error=None; job.session_id=None; db.commit()
        return out(job)
    from app.scanner.fiscal_check import api_guard_status
    guard=api_guard_status()
    job.status=FiscalQueueStatus.captcha
    if guard.get("blocked_until"):
        job.error=f"Публічний API ДПС тимчасово заблокований запобіжником до {guard['blocked_until']}. Доступна ручна CAPTCHA."
    elif guard.get("last_error"):
        job.error=f"Публічний API ДПС не повернув чек: {guard['last_error']}"
    else:
        job.error="Публічний API ДПС не повернув структуровані дані. Доступна ручна CAPTCHA."
    db.commit(); return out(job)

@router.post("/resolve")
async def resolve_from_public_api(payload: AddIn, db: Session = Depends(get_db)):
    """Import a fiscal URL through the token-authenticated DPS public API."""
    if payload.scan_job_id:
        scan_jobs.record_event(db,payload.scan_job_id,"dps",detail="запит до API ДПС")
    try: job=fiscal_queue.enqueue(db,payload.url,scan_job_id=payload.scan_job_id)
    except ValueError as e: raise HTTPException(400,str(e)) from e
    result=await _fetch_with_public_api(job,db)
    if result.get("receipt_id") and payload.scan_job_id:
        scan_jobs.attach_receipt(db,payload.scan_job_id,result["receipt_id"],method="tax_cabinet")
    return result

def _require_chk_all_web(url: str) -> None:
    """Only the ДПС one-time answer link may be fetched on the user's behalf."""
    parsed=urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "cabinet.tax.gov.ua" or parsed.path != "/ws/api_public/rro/chkAllWeb":
        raise HTTPException(400,"Дозволене лише HTTPS-посилання cabinet.tax.gov.ua/ws/api_public/rro/chkAllWeb")


def _ingest_out(result: xml_ingest.XmlIngestOutcome) -> dict:
    """The import answer, in the shape the queue UI already reads."""
    return {"id":result.queue_job_id,"url":result.viewer_url or None,
            "status":result.queue_status,"receipt_id":result.receipt_id,
            "error":None,"source":"xml","duplicate":result.duplicate,
            "outcome":result.outcome,"scan_job_id":result.scan_job_id,
            "scan_job_created":result.scan_job_created,
            "receipt_url":f"/receipts/{result.receipt_id}" if result.receipt_id else None,
            "fiscal_key":result.fiscal_key,"shop_name":result.shop_name,
            "purchased_at":result.purchased_at,"total_amount":result.total_amount,
            "message":result.message}


async def _ingest_xml(db: Session, value: str, *, filename: str | None = None,
                      size_bytes: int = 0, scan_job_id: str | None = None,
                      fiscal_job_id: str | None = None) -> dict:
    """Единая точка входа: любой XML ДПС, с привязкой к строке или без неё."""
    value=value.strip()
    if not value: raise HTTPException(400,"Вставте посилання chkAllWeb або XML-відповідь")
    if scan_job_id:
        detail=(f"завантажено {filename} ({size_bytes // 1024} КБ)" if filename
                else f"вставлено відповідь ДПС ({len(value)} симв.)")
        scan_jobs.record_event(db,scan_job_id,"import",detail=detail)
    source_url=""
    if value.startswith(("https://","http://")):
        _require_chk_all_web(value); source_url=value
        try:
            async with httpx.AsyncClient(timeout=20,headers={"Accept":"application/xml,text/xml,*/*"}) as client:
                response=await client.get(source_url); response.raise_for_status(); value=response.text
        except Exception as exc:
            raise HTTPException(400,"Одноразове посилання вже недійсне. Скопіюйте XML зі сторінки та вставте його сюди.") from exc
    try:
        # XML parsing, normalization and its bulk persistence can be expensive
        # for large fiscal documents. The worker owns a separate DB session;
        # request-bound SQLAlchemy sessions are not thread-safe.
        result=await asyncio.to_thread(
            xml_ingest.ingest_in_background, value, source_url=source_url, filename=filename,
            size_bytes=size_bytes or len(value.encode("utf-8")),
            scan_job_id=scan_job_id, fiscal_job_id=fiscal_job_id,
        )
    except xml_ingest.XmlTargetMismatch as exc:
        if scan_job_id:
            scan_jobs.record_event(db,scan_job_id,"import",outcome="fail",
                                   detail=f"файл іншого чека: {exc}")
        # 409, not 400: the file is fine, it is the row it was dropped on that
        # is wrong — the UI offers the universal import instead.
        raise HTTPException(409,str(exc)) from exc
    except xml_ingest.XmlIngestError as exc:
        if scan_job_id:
            scan_jobs.record_event(db,scan_job_id,"import",outcome="fail",
                                   detail=f"XML не прочитано: {exc}")
        raise HTTPException(400,f"Не вдалося прочитати відповідь ДПС: {exc}") from exc
    return _ingest_out(result)


@router.post("/import-response")
async def import_web_response(payload: ImportWebResponseIn, db: Session = Depends(get_db)):
    """Import XML (or its one-time chkAllWeb URL) obtained in a user's browser."""
    return await _ingest_xml(db,payload.value,scan_job_id=payload.scan_job_id,
                             fiscal_job_id=payload.fiscal_job_id)

@router.post("/import-response-file")
async def import_web_response_file(file: UploadFile = File(...),
                                   scan_job_id: str | None = Form(default=None),
                                   fiscal_job_id: str | None = Form(default=None),
                                   db: Session = Depends(get_db)):
    """Import a saved DPS XML response without sending it back to DPS."""
    filename=(file.filename or "").lower()
    if filename and not filename.endswith((".xml",".txt")):
        raise HTTPException(400,"Оберіть файл XML або TXT")
    data=await file.read(5*1024*1024+1)
    if len(data)>5*1024*1024:
        raise HTTPException(413,"Файл відповіді ДПС більший за 5 МБ")
    try: text=xml_ingest.decode_xml(data)
    except xml_ingest.XmlIngestError as exc:
        if scan_job_id:
            scan_jobs.record_event(db,scan_job_id,"import",outcome="fail",
                                   detail=f"{file.filename or 'XML'}: {exc}")
        raise HTTPException(400,str(exc)) from exc
    return await _ingest_xml(db,text,filename=file.filename,size_bytes=len(data),
                             scan_job_id=scan_job_id,fiscal_job_id=fiscal_job_id)

@router.post("/{job_id}/api")
async def run_public_api(job_id: str, db: Session = Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    return await _fetch_with_public_api(job,db)

@router.post("/{job_id}/run")
async def run(job_id: str, db: Session = Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    return await _fetch_with_public_api(job,db,require_token=False)

class ClickIn(BaseModel): x: float; y: float
@router.post("/{job_id}/browser/start")
async def browser_start(job_id:str,db:Session=Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    if job.status != FiscalQueueStatus.captcha: raise HTTPException(400,"CAPTCHA для цього чека не потрібна")
    if job.session_id:
        await fiscal_browser.close(job.session_id)
        job.session_id=None
    try:
        job.session_id=await fiscal_browser.start(job.id,job.url)
        job.error=None
    except Exception as e:
        message=str(e)
        if "profile appears to be in use" in message or "SingletonLock" in message:
            message="Профіль Chrome заблокований попереднім процесом. Повторіть запуск сеансу."
        job.error=f"Не вдалося запустити Google Chrome: {message}"; db.commit()
        raise HTTPException(503,job.error) from e
    db.commit(); return out(job)
@router.get("/{job_id}/browser/screenshot")
async def browser_screenshot(job_id:str,session_id:str,db:Session=Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    try: data=await fiscal_browser.screenshot(session_id,job_id)
    except ValueError as e:
        if job.session_id == session_id:
            job.session_id=None; db.commit()
        raise HTTPException(410,str(e)) from e
    return Response(data,media_type="image/jpeg",headers={"Cache-Control":"no-store"})
@router.post("/{job_id}/browser/click")
async def browser_click(job_id:str,session_id:str,p:ClickIn,db:Session=Depends(get_db)):
    try: await fiscal_browser.click(session_id,job_id,p.x,p.y)
    except ValueError as e: raise HTTPException(410,str(e)) from e
    return {"ok":True}
@router.post("/{job_id}/browser/reload")
async def browser_reload(job_id:str,session_id:str,db:Session=Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    try: await fiscal_browser.reload(session_id,job_id)
    except ValueError as e: raise HTTPException(410,str(e)) from e
    return await fiscal_browser.state(session_id,job_id)
@router.get("/{job_id}/browser/state")
async def browser_state(job_id:str,session_id:str,db:Session=Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    try: return await fiscal_browser.state(session_id,job_id)
    except ValueError as e:
        # Forget the dead session (same as the screenshot route) so the job stops
        # advertising it and the queue can be restarted cleanly.
        if job.session_id == session_id:
            job.session_id=None; db.commit()
        raise HTTPException(410,str(e)) from e
@router.post("/{job_id}/browser/check")
async def browser_check(job_id:str,session_id:str,db:Session=Depends(get_db)):
    job=db.get(FiscalQueueJob,job_id)
    if not job: raise HTTPException(404,"Чек не знайдено")
    try: receipt=await fiscal_browser.result(session_id,job_id,job.url)
    except Exception as e: raise HTTPException(400,f"ДПС повернула непридатний чек: {e}") from e
    if not receipt: return out(job)
    saved=_persist_receipt(db,_scanner_account(db),receipt); job.status=FiscalQueueStatus.imported
    job.receipt_id=saved.id; job.error=None; job.session_id=None; db.commit()
    await fiscal_browser.close(session_id); return out(job)
