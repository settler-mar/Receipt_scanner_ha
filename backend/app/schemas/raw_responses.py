from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RawResponseStoreSummary(BaseModel):
    store_code: str
    store_name: str | None = None
    count: int
    bytes: int
    oldest_at: datetime | None = None
    newest_at: datetime | None = None


class RawResponseSummary(BaseModel):
    total_count: int
    total_bytes: int
    stores: list[RawResponseStoreSummary]


class RawResponseSettings(BaseModel):
    retention_days: int = Field(default=90, ge=1, le=3650)
    auto_cleanup: bool = False


class RawResponseCleanup(BaseModel):
    mode: str = Field(pattern="^(all|retention)$")


class RawResponseCleanupResult(BaseModel):
    removed: int
