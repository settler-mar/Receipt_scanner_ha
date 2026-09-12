from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.scanner import FiscalQueueJob, FiscalQueueStatus
from app.scanner import tax_cabinet
from app.scanner.cabinet_check import parse_cabinet_check_xml
from app.scanner.fiscal_check import parse_check_xml
from app.scanner.fiscal_qr import parse_fiscal_url
from app.services import fiscal_queue, scan_jobs, tax_cabinet_settings
from app.services import fiscal_browser
from app.api.inbox import _persist_receipt, _scanner_account
from app.services.manual_dedup import find_duplicate
from app.core.time import KYIV, as_utc

router = APIRouter(prefix="/api/fiscal-queue", tags=["fiscal queue"])

# `scan_job_id` ties the import back to the inbox row it was started from, so
# that row can show the receipt instead of staying on "needs_review".
class AddIn(BaseModel):
    url: str
    scan_job_id: str | None = None
class ImportWebResponseIn(BaseModel):
    value: str
    scan_job_id: str | None = None

def out(j: FiscalQueueJob):
    return {"id":j.id,"url":j.url,"source":j.source,"status":j.status.value,
            "attempts":j.attempts,"receipt_id":j.receipt_id,"scan_job_id":j.scan_job_id,"error":j.error,
            # Only a session that still exists in this process is worth showing:
            # a browser session does not survive a restart, and a stale id would
            # make the UI poll a screen that can never come back.
            "session_id":j.session_id if fiscal_browser.is_live(j.session_id,j.id) else None,"created_at":as_utc(j.created_at),"updated_at":as_utc(j.updated_at),
            "receipt_url":f"/receipts/{j.receipt_id}" if j.receipt_id else None}

@router.get("")
def list_jobs(db: Session = Depends(get_db)):
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
    try: job=fiscal_queue.enqueue(db,payload.url,scan_job_id=payload.scan_job_id)
    except ValueError as e: raise HTTPException(400,str(e)) from e
    result=await _fetch_with_public_api(job,db)
    if result.get("receipt_id") and payload.scan_job_id:
        scan_jobs.attach_receipt(db,payload.scan_job_id,result["receipt_id"],method="tax_cabinet")
    return result

def _viewer_url_from_web_response(url: str, root: ET.Element, check=None) -> str:
    query=parse_qs(urlparse(url).query) if url else {}
    fn=(query.get("fn") or [root.findtext("fn") or getattr(check,"fiscal_number","") or ""])[0]
    params={key:(query.get(key) or [""])[0] for key in ("date","id","sm")}
    if check:
        params["id"]=params["id"] or check.document_id or ""
        params["sm"]=params["sm"] or (f"{check.total:.2f}" if check.total else "")
        if not params["date"] and check.issued_at:
            local=check.issued_at.astimezone(KYIV)
            params["date"]=local.strftime("%Y%m%d")
            params["time"]=local.strftime("%H%M%S")
    params["fn"]=fn
    return "https://cabinet.tax.gov.ua/cashregs/check?"+urlencode({k:v for k,v in params.items() if v})

def _link_scan_job(db: Session, scan_job_id: str | None, receipt_id: str, receipt) -> None:
    """Show the imported receipt on the inbox row the import was started from."""
    if not scan_job_id:
        return
    method=(receipt.raw or {}).get("method") or "tax_cabinet"
    scan_jobs.attach_receipt(db,scan_job_id,receipt_id,method=method)


def _parse_web_response(body: str, source_url: str = ""):
    start=body.find("<Chk")
    if start < 0 and "<CHECK" in body:
        # The cabinet's own per-receipt download: already decoded, and with no
        # fiscal document number, so there is no viewer URL to queue against.
        receipt=parse_cabinet_check_xml(body)
        if receipt is None: raise ValueError("Файл схожий на <CHECK>, але прочитати його не вдалося")
        return receipt,""
    if start < 0:
        # "Save page" on the DPS result downloads the decoded fiscal RQ
        # document directly, rather than the outer Chk/base64 envelope.
        rq_start=body.find("<RQ")
        declaration=body.find("<?xml")
        if rq_start < 0: raise ValueError("У файлі немає фіскального XML <RQ> або відповіді <Chk>")
        raw=body[declaration if 0 <= declaration < rq_start else rq_start:]
        # parse_check_xml already resolves ПН/ЄДРПОУ to the retail brand.
        check=parse_check_xml(raw)
        viewer_url=_viewer_url_from_web_response(source_url,ET.Element("RQ"),check)
        check.raw_url=viewer_url
        return check.to_normalized_receipt(),viewer_url
    root=ET.fromstring(body[start:])
    xml_b64=(root.findtext("checkXml") or "").strip()
    if not xml_b64: raise ValueError("ДПС не повернула поле checkXml")
    plain_b64=(root.findtext("check") or "").strip()
    plain=None
    if plain_b64:
        plain_bytes=base64.b64decode(plain_b64)
        try: plain=plain_bytes.decode("utf-8")
        except UnicodeDecodeError: plain=plain_bytes.decode("cp1251","replace")
    check=parse_check_xml(base64.b64decode(xml_b64),plaintext=plain)
    viewer_url=_viewer_url_from_web_response(source_url,root,check)
    check.raw_url=viewer_url
    return check.to_normalized_receipt(),viewer_url

@router.post("/import-response")
async def import_web_response(payload: ImportWebResponseIn, db: Session = Depends(get_db)):
    """Import XML (or its one-time chkAllWeb URL) obtained in a user's browser."""
    value=payload.value.strip()
    if not value: raise HTTPException(400,"Вставте посилання chkAllWeb або XML-відповідь")
    source_url=""
    if value.startswith(("https://","http://")):
        parsed=urlparse(value)
        if parsed.scheme != "https" or parsed.hostname != "cabinet.tax.gov.ua" or parsed.path != "/ws/api_public/rro/chkAllWeb":
            raise HTTPException(400,"Дозволене лише HTTPS-посилання cabinet.tax.gov.ua/ws/api_public/rro/chkAllWeb")
        source_url=value
        try:
            async with httpx.AsyncClient(timeout=20,headers={"Accept":"application/xml,text/xml,*/*"}) as client:
                response=await client.get(value); response.raise_for_status(); value=response.text
        except Exception as exc:
            raise HTTPException(400,"Одноразове посилання вже недійсне. Скопіюйте XML зі сторінки та вставте його сюди.") from exc
    try: receipt,viewer_url=_parse_web_response(value,source_url)
    except Exception as exc: raise HTTPException(400,f"Не вдалося прочитати відповідь ДПС: {exc}") from exc
    account=_scanner_account(db)
    # Decide before persisting: afterwards a merge is indistinguishable from an
    # insert, and the user needs to be told which of the two happened.
    duplicate=find_duplicate(db,receipt) is not None
    if not viewer_url:
        # A document that names no fiscal check number has nothing to queue
        # against — it is already the answer the queue exists to fetch.
        saved=_persist_receipt(db,account,receipt)
        db.commit()
        _link_scan_job(db,payload.scan_job_id,saved.id,receipt)
        return {"id":None,"url":None,"status":FiscalQueueStatus.imported.value,
                "receipt_id":saved.id,"error":None,"source":"xml","duplicate":duplicate}
    try: job=fiscal_queue.enqueue(db,viewer_url,scan_job_id=payload.scan_job_id)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc
    saved=_persist_receipt(db,account,receipt)
    if job.session_id: await fiscal_browser.close(job.session_id)
    job.status=FiscalQueueStatus.imported; job.receipt_id=saved.id
    job.error=None; job.session_id=None; db.commit()
    _link_scan_job(db,payload.scan_job_id,saved.id,receipt)
    return {**out(job),"duplicate":duplicate}

@router.post("/import-response-file")
async def import_web_response_file(file: UploadFile = File(...),
                                   scan_job_id: str | None = Form(default=None),
                                   db: Session = Depends(get_db)):
    """Import a saved DPS XML response without sending it back to DPS."""
    filename=(file.filename or "").lower()
    if filename and not filename.endswith((".xml",".txt")):
        raise HTTPException(400,"Оберіть файл XML або TXT")
    data=await file.read(5*1024*1024+1)
    if len(data)>5*1024*1024:
        raise HTTPException(413,"Файл відповіді ДПС більший за 5 МБ")
    try: text=data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try: text=data.decode("cp1251")
        except UnicodeDecodeError as exc: raise HTTPException(400,"Невідоме кодування XML-файлу") from exc
    return await import_web_response(
        ImportWebResponseIn(value=text,scan_job_id=scan_job_id),db)

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
