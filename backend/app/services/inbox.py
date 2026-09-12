"""Personal-document branch of the upload router (ROADMAP.md Крок 3, "Документи").

Hard rule: this module must never import app.services.ai (or app.scanner, which
pulls it in transitively via the API layer). A personal document — a contract, an
ID page, an insurance form — never goes near an external model. There is a test
that asserts this (tests/test_inbox.py).

Storage: when Google Drive is connected and an inbox folder is chosen the bytes
are uploaded straight there (app.services.drive) and not kept locally; otherwise
they land in a local holding folder (`data/inbox/`). `InboxDocument.storage`
records which path was taken; a Drive failure falls back to local rather than
losing the upload.
"""
from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import BASE_DIR
from app.models.models import AppSetting
from app.models.scanner import InboxDocument, InboxStatus
from app.services import drive
from app.scanner.aspect import (
    DEFAULT_COLOR_MAX_NONGRAY_FRAC,
    DEFAULT_COLOR_MAX_SATURATION,
    DEFAULT_RECEIPT_RATIO,
    ImageShape,
)

INBOX_DIR = BASE_DIR / "data" / "inbox"

DRIVE_CONFIG_KEY = "drive_config"
INBOX_CONFIG_KEY = "inbox_config"


class DriveConfig(BaseModel):
    connected: bool = False
    # Base folder for everything document-related — where the folder browser
    # starts and where new folders are created. Optional; "My Drive" root if unset.
    root_folder_id: str | None = None
    root_folder_name: str | None = None       # display cache, not authoritative
    root_folder_color: str | None = None      # folderColorRgb, mirrors Drive
    # Sub-folder that fresh uploads land in ("Необроблені"). Falls back to
    # root_folder_id, then to Drive root, if unset.
    inbox_folder_id: str | None = None
    inbox_folder_name: str | None = None      # display cache, not authoritative
    inbox_folder_color: str | None = None     # folderColorRgb, mirrors Drive


class InboxConfig(BaseModel):
    # h/w threshold that splits "receipt" from "document" on upload.
    receipt_ratio: float = DEFAULT_RECEIPT_RATIO
    # Colour signal: at/below both of these an image counts as monochrome and is
    # routed to the receipt pipeline regardless of shape (ROADMAP.md Крок 3).
    color_max_saturation: float = DEFAULT_COLOR_MAX_SATURATION
    color_max_nongray_fraction: float = DEFAULT_COLOR_MAX_NONGRAY_FRAC
    max_file_mb: float = 25.0


class DriveNotConfigured(RuntimeError):
    """Google Drive isn't connected yet — the caller should fall back to local
    storage or tell the user to finish the Drive setup on the Settings page."""


def _load(db: Session, key: str, model):
    row = db.get(AppSetting, key)
    if row is None or not row.value:
        return model()
    try:
        return model.model_validate(row.value)
    except Exception:
        return model()


def load_drive_config(db: Session) -> DriveConfig:
    cfg = _load(db, DRIVE_CONFIG_KEY, DriveConfig)
    # `connected` is derived, not trusted from the stored blob: it is true iff a
    # usable refresh token actually exists.
    cfg.connected = drive.is_connected(db)
    return cfg


def load_inbox_config(db: Session) -> InboxConfig:
    return _load(db, INBOX_CONFIG_KEY, InboxConfig)


def save_config(db: Session, key: str, value: BaseModel) -> None:
    row = db.get(AppSetting, key)
    payload = value.model_dump()
    if row is None:
        db.add(AppSetting(key=key, value=payload))
    else:
        row.value = payload
    db.commit()


def _local_store(data: bytes, ext: str) -> str:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (INBOX_DIR / name).write_bytes(data)
    return name


async def store_document(
    db: Session,
    *,
    data: bytes,
    original_filename: str | None,
    content_type: str | None,
    shape: ImageShape | None,
) -> InboxDocument:
    """Persist an incoming wide image and its InboxDocument row.

    If Drive is connected and an inbox folder is chosen, the bytes go straight
    there and nothing is kept locally; otherwise they land in ``data/inbox/``.
    A Drive failure falls back to local rather than losing the upload.
    """
    ext = Path(original_filename or "").suffix.lower() or ".bin"
    cfg = load_drive_config(db)

    doc = InboxDocument(
        original_filename=original_filename,
        content_type=content_type,
        width=shape.width if shape else None,
        height=shape.height if shape else None,
        ratio=round(shape.ratio, 4) if shape else None,
        status=InboxStatus.unprocessed,
    )
    upload_parent = cfg.inbox_folder_id or cfg.root_folder_id
    if cfg.connected and upload_parent:
        try:
            doc.drive_file_id = await drive.upload_file(
                db, data=data, name=original_filename or f"{doc.id}{ext}",
                parent_id=upload_parent, mime=content_type,
            )
            doc.storage = "drive"
        except drive.DriveError:
            doc.local_filename = _local_store(data, ext)
            doc.storage = "local"
    else:
        doc.local_filename = _local_store(data, ext)
        doc.storage = "local"

    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def local_path(doc: InboxDocument) -> Path | None:
    if doc.storage != "local" or not doc.local_filename:
        return None
    return INBOX_DIR / doc.local_filename


async def process_document(
    db: Session, doc: InboxDocument, *, final_name: str, target_folder_id: str | None
) -> InboxDocument:
    """Give the document its final name + location and close it out."""
    final_name = final_name.strip()
    if not final_name:
        raise ValueError("final_name is required")

    if doc.storage == "drive":
        if not doc.drive_file_id:
            raise DriveNotConfigured("Файл не має ідентифікатора на Drive")
        try:
            await drive.move_and_rename(
                db, file_id=doc.drive_file_id, name=final_name,
                target_parent_id=target_folder_id,
            )
        except drive.DriveError as exc:
            raise DriveNotConfigured(str(exc)) from exc
    else:
        path = local_path(doc)
        if path and path.exists():
            safe = final_name.replace("/", "_")
            if not Path(safe).suffix:
                safe += path.suffix
            path.rename(INBOX_DIR / safe)
            doc.local_filename = safe

    doc.final_name = final_name
    doc.target_folder_id = target_folder_id
    doc.status = InboxStatus.done
    doc.processed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(doc)
    return doc


# --------------------------------------------------------------------------- #
# Google Drive folder navigation (thin wrappers over app.services.drive)
# --------------------------------------------------------------------------- #
class DriveFolder(BaseModel):
    id: str
    name: str
    color: str | None = None       # folderColorRgb

    @classmethod
    def of(cls, f: dict) -> "DriveFolder":
        return cls(id=f["id"], name=f["name"], color=(f.get("folderColorRgb") or "").lower() or None)


async def drive_tree(db: Session, parent_id: str | None = None) -> list[DriveFolder]:
    if not load_drive_config(db).connected:
        raise DriveNotConfigured(
            "Google Drive не під'єднано — натисніть «Підключити» в Налаштуваннях"
        )
    try:
        return [DriveFolder.of(f) for f in await drive.list_folders(db, parent_id)]
    except drive.DriveError as exc:
        raise DriveNotConfigured(str(exc)) from exc


async def create_drive_folder(
    db: Session, *, name: str, parent_id: str | None, color: str | None = None
) -> DriveFolder:
    if not load_drive_config(db).connected:
        raise DriveNotConfigured("Google Drive не під'єднано")
    try:
        return DriveFolder.of(await drive.create_folder(db, name=name, parent_id=parent_id, color=color))
    except drive.DriveError as exc:
        raise DriveNotConfigured(str(exc)) from exc


async def set_folder_color(db: Session, folder_id: str, color: str | None) -> str | None:
    if not load_drive_config(db).connected:
        raise DriveNotConfigured("Google Drive не під'єднано")
    try:
        applied = await drive.set_folder_color(db, folder_id, color)
    except drive.DriveError as exc:
        raise DriveNotConfigured(str(exc)) from exc
    # Keep the drive_config display cache in sync when it's the root/inbox folder.
    cfg = load_drive_config(db)
    changed = False
    if cfg.root_folder_id == folder_id:
        cfg.root_folder_color, changed = applied, True
    if cfg.inbox_folder_id == folder_id:
        cfg.inbox_folder_color, changed = applied, True
    if changed:
        save_config(db, DRIVE_CONFIG_KEY, cfg)
    return applied
