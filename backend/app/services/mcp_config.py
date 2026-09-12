"""Runtime config for the own MCP server (ROADMAP.md Крок 4).

Stored in `app_settings` under `mcp_config` (same pattern as `ai_config`), edited
on the Settings page. Lets the user:
  * turn the whole MCP server on/off;
  * set an access level **per tool** — off / read / read+write.

`app/mcp_server.py` consults `load_config()` on every `tools/list` and
`tools/call`, so changes take effect without a restart.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.models.models import AppSetting

logger = logging.getLogger(__name__)

CONFIG_KEY = "mcp_config"

# Per-tool access level the user picks:
#   "off"   — tool hidden from clients and refused on call
#   "read"  — tool available if it only reads; a write tool stays hidden
#   "write" — tool available (read or write)
McpAccess = Literal["off", "read", "write"]

# name -> inherent kind ("read" | "write"). Single source of truth for which
# tools exist and whether calling one mutates the DB; mirrors the @mcp.tool()
# functions in mcp_server.py.
TOOLS: dict[str, Literal["read", "write"]] = {
    "search_products": "read",
    "spending_summary": "read",
    "list_receipts": "read",
    "suggest_store": "read",
    "add_to_shopping_list": "write",
    "add_receipt": "write",
    "add_fuel_purchase": "write",
    "capture_text": "write",
}

TOOL_LABELS: dict[str, str] = {
    "search_products": "Пошук товарів і цін в історії чеків",
    "spending_summary": "Зведення витрат (усього / по мережах / категоріях / місяцях)",
    "list_receipts": "Перелік чеків",
    "suggest_store": "Підбір магазину за списком покупок",
    "add_to_shopping_list": "Додати позицію в список покупок",
    "add_receipt": "Записати покупку (чек) у базу",
    "add_fuel_purchase": "Записати заправку (паливо) і прив'язати до АЗС",
    "capture_text": "Розібрати вільну фразу («додай чек …», «додай в список …»)",
}


def default_access(name: str) -> McpAccess:
    """Safe default for a tool with no stored setting: read tools open at
    'read', write tools closed ('off') until explicitly granted."""
    return "read" if TOOLS.get(name) == "read" else "off"


def _coerce_access(name: str, value: object) -> McpAccess:
    # Seamless upgrade from the old shape (`tools: {name: bool}` + a global
    # `mode`): a bare True becomes "read" (never silently grants write — the
    # user re-picks "write" for the tools they want), False becomes "off".
    if isinstance(value, bool):
        return "read" if value else "off"
    if value in ("off", "read", "write"):
        return value  # type: ignore[return-value]
    return default_access(name)


class McpConfig(BaseModel):
    enabled: bool = True
    # per-tool access level; missing keys fall back to default_access, unknown
    # keys are dropped
    tools: dict[str, McpAccess] = Field(
        default_factory=lambda: {name: default_access(name) for name in TOOLS}
    )

    @field_validator("tools", mode="before")
    @classmethod
    def _clean_tools(cls, v: object) -> dict[str, McpAccess]:
        v = v if isinstance(v, dict) else {}
        return {name: _coerce_access(name, v.get(name)) for name in TOOLS}

    def access_for(self, name: str) -> McpAccess:
        return self.tools.get(name, default_access(name))

    def tool_available(self, name: str) -> bool:
        """Server on, tool not 'off', and — for a write tool — level is 'write'."""
        if not self.enabled or name not in TOOLS:
            return False
        access = self.access_for(name)
        if access == "off":
            return False
        if TOOLS[name] == "write" and access != "write":
            return False
        return True


def load_config(db: Session) -> McpConfig:
    row = db.get(AppSetting, CONFIG_KEY)
    if row is None or not row.value:
        return McpConfig()
    try:
        return McpConfig.model_validate(row.value)
    except Exception:  # tolerate a partially-written / older-shaped blob
        logger.warning("mcp_config in app_settings is malformed, using defaults", exc_info=True)
        return McpConfig()


def save_config(db: Session, cfg: McpConfig) -> McpConfig:
    row = db.get(AppSetting, CONFIG_KEY)
    payload = cfg.model_dump()
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=payload))
    else:
        row.value = payload
    db.commit()
    return cfg


def tools_meta(cfg: McpConfig) -> list[dict]:
    """For the Settings page: every tool with its inherent kind, label, current
    level and whether a client can reach it right now. The UI is a single
    checkbox per tool — checked maps to "read" (read tool) or "write" (write
    tool), unchecked to "off"."""
    return [
        {
            "name": name,
            "kind": kind,
            "label": TOOL_LABELS.get(name, name),
            "access": cfg.access_for(name),
            "available": cfg.tool_available(name),
        }
        for name, kind in TOOLS.items()
    ]
