from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.models import DuplicateGroup
from app.schemas.schemas import DuplicateGroupOut, DuplicateMergeChoices, DuplicateRunOut
from app.services import duplicates

router = APIRouter(prefix="/api/duplicates", tags=["duplicates"])


@router.post("/run", response_model=DuplicateRunOut)
def run_duplicate_search(db: Session = Depends(get_db)):
    try:
        return duplicates.run_duplicate_search(db, trigger="manual")
    except duplicates.DuplicateRunAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/runs", response_model=list[DuplicateRunOut])
def list_runs(db: Session = Depends(get_db)):
    return duplicates.latest_runs(db)


@router.get("/groups", response_model=list[DuplicateGroupOut])
def list_groups(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return duplicates.list_groups(db, status=status)


@router.get("/groups/{group_id}", response_model=DuplicateGroupOut)
def get_group(group_id: str, db: Session = Depends(get_db)):
    group = db.get(DuplicateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Duplicate group not found")
    return duplicates.group_payload(db, group)


@router.post("/groups/{group_id}/merge", response_model=DuplicateGroupOut)
def merge_group(group_id: str, payload: DuplicateMergeChoices | None = None, db: Session = Depends(get_db)):
    try:
        group = duplicates.merge_group(db, group_id, choices=payload.model_dump() if payload else None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return duplicates.group_payload(db, group)


@router.post("/groups/{group_id}/reject", response_model=DuplicateGroupOut)
def reject_group(group_id: str, db: Session = Depends(get_db)):
    try:
        group = duplicates.reject_group(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return duplicates.group_payload(db, group)


@router.post("/groups/{group_id}/rollback", response_model=DuplicateGroupOut)
def rollback_group(group_id: str, db: Session = Depends(get_db)):
    try:
        group = duplicates.rollback_group(db, group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return duplicates.group_payload(db, group)
