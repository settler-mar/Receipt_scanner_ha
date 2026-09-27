# Store / Receipt Provider Adapter

Adapters are pure API clients: no DB access, no persistence. The collector
(`backend/app/services/collector.py`) persists what they return.

## Read first (only these)

- `backend/app/adapters/base.py` — `BaseAdapter`, `NormalizedReceipt`,
  `NormalizedItem`, `ROTATED_ACCESS_TOKEN_KEY`.
- The closest existing adapter: token API -> `atb.py`/`silpo.py`,
  fuel/loyalty -> `wog.py`/`upg.py`/`ukrnafta.py`, marketplace/orders ->
  `rozetka.py`/`prom.py`/`aliexpress.py`, offline+online -> `eva.py`/`fora.py`.
- Its test: `backend/tests/test_<provider>.py`.

## Checklist

1. `backend/app/adapters/<store>.py` implementing the base contract
   (login / refresh / fetch receipts + items).
2. Register in `backend/app/adapters/registry.py` (`_ADAPTERS["<code>"]`).
3. Add the store row to `STORES` in `backend/app/seed.py`
   (`code, name, auth_type, network_type`) — the code must match the registry key.
4. Frontend icon/label if the store must be pickable: `frontend/src/lib/storeIcons.ts`.
5. Test `backend/tests/test_<store>.py` with recorded JSON fixtures — no live
   network in tests.

## Normalization rules

- Money in UAH floats; foreign currency sets `currency` + `exchange_rate_to_uah`.
- Bonuses in currency units, not points. WOG/UPG-style APIs report points ×100 —
  divide by 100 in the adapter (see `wog.py`, migration `a4b7c2e91d35`).
- Timestamps: UTC-naive `datetime` internally; display TZ (Europe/Kyiv) is the
  frontend's job.
- `channel` = `"offline"` unless the provider genuinely has an online branch.
- Leave `store_code` None in store adapters; only fiscal/ДПС ingest sets it.
- Set `total_discount` only when the API gives an authoritative order-level total.
- Keep the accounting boundary explicit: `NormalizedItem` is only a product
  line. Delivery, tax, service fees, coupons, coins, seller adjustments and
  any other order-level amount belong in `NormalizedReceipt.components` as
  signed values (positive charge, negative reduction), with a stable `code`,
  display `label`, provider `source`, and original `raw` evidence. Do not add
  new component types to the ORM for each store. If the complete breakdown
  still does not reconcile to the order total, put the signed remainder in
  `total_adjustment` and explain it in `adjustment_reason`; never silently
  manufacture a product discount.

## Failure handling

Timeouts, auth failure (401/403) and parse failure must raise explicit, typed
errors with a human-readable message — the collector surfaces them in the UI and
in `/logs`. Never log tokens, cookies, phone numbers, or receipt personal data.
