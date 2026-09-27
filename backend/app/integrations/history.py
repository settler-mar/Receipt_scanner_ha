"""Reusable state contract for incremental and full-history imports."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FullHistoryRequest:
    load_all: bool
    known_keys: set[str]
    initial_backfill: bool
    refresh_details: bool
    detail_known_keys: set[str]


class FullHistoryImport:
    """Namespaced credential marker plus transient adapter options."""

    FORCE_ALL_OPTION = "_history_force_all"
    LOAD_ALL_OPTION = "_history_load_all"
    STOP_KEYS_OPTION = "_history_stop_keys"
    REFRESH_DETAILS_OPTION = "_history_refresh_details"
    DETAIL_KNOWN_KEYS_OPTION = "_history_detail_known_keys"
    COMPLETE_RESULT = "_history_complete"

    @staticmethod
    def marker_key(store_code: str) -> str:
        return f"{store_code}_full_history_loaded"

    @classmethod
    def options(
        cls,
        known_keys: list[str],
        *,
        refresh_details: bool = False,
        detail_known_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            cls.FORCE_ALL_OPTION: True,
            cls.LOAD_ALL_OPTION: True,
            cls.STOP_KEYS_OPTION: known_keys,
            cls.REFRESH_DETAILS_OPTION: refresh_details,
            cls.DETAIL_KNOWN_KEYS_OPTION: detail_known_keys or [],
        }

    @classmethod
    def consume(cls, extra: dict, store_code: str) -> FullHistoryRequest:
        load_all = bool(extra.pop(cls.LOAD_ALL_OPTION, False))
        known_keys = {
            str(value)
            for value in extra.pop(cls.STOP_KEYS_OPTION, [])
            if value not in (None, "")
        }
        refresh_details = bool(extra.pop(cls.REFRESH_DETAILS_OPTION, False))
        detail_known_keys = {
            str(value)
            for value in extra.pop(cls.DETAIL_KNOWN_KEYS_OPTION, [])
            if value not in (None, "")
        }
        return FullHistoryRequest(
            load_all=load_all,
            known_keys=known_keys,
            initial_backfill=(load_all and not bool(extra.get(cls.marker_key(store_code)))) or refresh_details,
            refresh_details=refresh_details,
            detail_known_keys=detail_known_keys,
        )

    @classmethod
    def mark_complete(cls, extra: dict) -> None:
        extra[cls.COMPLETE_RESULT] = True
