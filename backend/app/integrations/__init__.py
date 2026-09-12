"""Public building blocks for browser-backed store integrations."""

from app.integrations.browser import (
    BrowserSession,
    BrowserSessionConfig,
    JsonResponseCapture,
    PageCollectionResult,
    PageInfo,
    PaginatedRecordCollector,
)
from app.integrations.order_delivery import (
    DeliveryUpdate,
    OrderDeliveryUpdater,
    OrderTrackingAdapter,
)
from app.integrations.history import FullHistoryImport, FullHistoryRequest

__all__ = [
    "BrowserSession",
    "BrowserSessionConfig",
    "DeliveryUpdate",
    "FullHistoryImport",
    "FullHistoryRequest",
    "JsonResponseCapture",
    "OrderDeliveryUpdater",
    "OrderTrackingAdapter",
    "PageCollectionResult",
    "PageInfo",
    "PaginatedRecordCollector",
]
