import asyncio
from types import SimpleNamespace

import pytest

from app.integrations import (
    BrowserSession,
    DeliveryUpdate,
    FullHistoryImport,
    JsonResponseCapture,
    OrderDeliveryUpdater,
    PageInfo,
    PaginatedRecordCollector,
)


def test_browser_session_validates_portable_storage_state():
    assert BrowserSession.parse_storage_state('{"cookies": [], "origins": []}')["cookies"] == []
    with pytest.raises(ValueError):
        BrowserSession.parse_storage_state('{"origins": []}')


def test_full_history_state_is_namespaced_and_consumes_transient_options():
    extra = {
        FullHistoryImport.marker_key("store-a"): True,
        **FullHistoryImport.options(["10", "20"]),
    }
    request = FullHistoryImport.consume(extra, "store-a")

    assert request.load_all is True
    assert request.known_keys == {"10", "20"}
    assert request.initial_backfill is False
    assert request.refresh_details is False
    assert request.detail_known_keys == set()
    assert FullHistoryImport.LOAD_ALL_OPTION not in extra
    assert FullHistoryImport.STOP_KEYS_OPTION not in extra


def test_full_history_can_refresh_provider_details_after_history_is_loaded():
    extra = {
        FullHistoryImport.marker_key("store-a"): True,
        **FullHistoryImport.options(["10"], refresh_details=True),
    }
    request = FullHistoryImport.consume(extra, "store-a")

    assert request.refresh_details is True
    assert request.initial_backfill is True
    assert request.detail_known_keys == set()
    assert FullHistoryImport.REFRESH_DETAILS_OPTION not in extra


def test_paginated_collector_stops_at_known_record_without_store_specific_code():
    capture = JsonResponseCapture(lambda _url: True)
    capture.payloads = [{"rows": [{"id": "new"}, {"id": "known"}, {"id": "old"}]}]
    collector = PaginatedRecordCollector(
        record_groups=lambda payload: [payload["rows"]],
        record_key=lambda row: row["id"],
        page_info=lambda _payload: PageInfo(False, "More"),
    )
    result = asyncio.run(collector.collect(
        SimpleNamespace(),
        capture,
        paginate=True,
        known_keys={"known"},
        stop_on_known=True,
    ))

    assert list(result.records) == ["new"]
    assert result.stopped_on_known is True


def test_delivery_updater_merges_numbers_and_never_removes_existing_ones():
    receipt = SimpleNamespace(
        external_id="order-1",
        order_status="Awaiting delivery",
        tracking_numbers=["TRACK-1"],
    )
    changed = OrderDeliveryUpdater.apply(
        [receipt],
        {"order-1": DeliveryUpdate(status="Completed", tracking_numbers=["TRACK-1", "TRACK-2"])},
    )

    assert changed == 1
    assert receipt.order_status == "Completed"
    assert receipt.tracking_numbers == ["TRACK-1", "TRACK-2"]
