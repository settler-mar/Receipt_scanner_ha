import datetime as dt

from app.services import raw_responses


def test_raw_response_store_is_partitioned_and_summarized(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_responses, "RAW_ROOT", tmp_path / "raw_responses")
    captured = dt.datetime(2026, 9, 20, 12, tzinfo=dt.timezone.utc)

    path = raw_responses.store(
        store_code="aliexpress",
        account_id="account-1",
        collection_run_id="run-1",
        external_id="order-1",
        payload={"shippingFee": "3.00"},
        captured_at=captured,
    )

    assert path is not None
    assert path.parent == tmp_path / "raw_responses" / "aliexpress" / "2026" / "09"
    summary = raw_responses.summary()
    assert summary["total_count"] == 1
    assert summary["stores"][0]["store_code"] == "aliexpress"
    assert summary["stores"][0]["oldest_at"] == captured.isoformat()
    assert summary["stores"][0]["newest_at"] == captured.isoformat()


def test_raw_response_cleanup_removes_only_expired_files(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_responses, "RAW_ROOT", tmp_path / "raw_responses")
    now = dt.datetime.now(dt.timezone.utc)
    raw_responses.store(
        store_code="atb",
        account_id="account-1",
        collection_run_id="run-old",
        external_id="old",
        payload={"ok": True},
        captured_at=now - dt.timedelta(days=91),
    )
    raw_responses.store(
        store_code="atb",
        account_id="account-1",
        collection_run_id="run-new",
        external_id="new",
        payload={"ok": True},
        captured_at=now - dt.timedelta(days=2),
    )

    assert raw_responses.cleanup_older_than(90) == 1
    assert raw_responses.summary()["total_count"] == 1
