# 09 — Verification, and the upgrade risk

**Files:** `test/test_flattrade_squareoff_safeguards.py`,
`test/test_flattrade_funds_cache.py`, `test/test_flattrade_protocol.py`,
`kb/gotchas.md`

This document records what actually verifies the modifications, how to re-verify
without a broker, and what an OpenAlgo upgrade will silently undo.

---

## 1. The tests that exist

### 1.1 `test/test_flattrade_squareoff_safeguards.py`

A **standalone smoke test**, not a pytest module. It runs with the server's own
virtualenv:

```
cd <server root>
.\.venv\Scripts\python.exe test\test_flattrade_squareoff_safeguards.py
```

Exit code 0 = every check passed; 1 = at least one failed. It prints `OK` / `FAIL`
per check and a summary.

What it pins:

| Section | Checks |
|---|---|
| 1. Empty vs unreadable book | `"no data"` → `[]`; Session Expired / auth failure / gateway error / bare `Not_Ok` → `BrokerReadError`; `None` → raises; list keeps only dict rows; single-row dict wrapped |
| 2a. Tick rounding | floor/ceil/nearest at 0.05; the `102.0 / 0.05` float trap; no-tick 2dp fallback; SELL floors, BUY ceils |
| 2b. Marketable price | SELL stays at/below the bid; SELL crosses down to a low bid; BUY stays at/above the ask; BUY crosses up to a high ask; no touch → protected price, still tick-valid |
| 3. Working-SELL ledger | half-filled SELL reports `remaining`; fully-filled excluded; BUY excluded; COMPLETE excluded; other contracts kept separately |
| 4a. Oversell guard | one close, SELL, quantity == long, no cancel, success |
| 4b. Stale SELL | cancelled **and** replaced (no deadlock), full quantity, success |
| 4c. Uncoverable SELL | cancel attempted, **no second SELL**, `covered` branch |
| 4d. Rejected close | non-200, `status: error`, failure detail carried |
| 5. MKT guard | `MPPPriceUnavailable` is an Exception |

The end-to-end sections stub the broker I/O by **replacing module attributes**
(`oa.get_positions`, `oa.get_order_book`, `oa.get_symbol`, `oa.place_order_api`,
`oa.cancel_order`) and set `oa._CLOSE_CANCEL_SETTLE = 0` to keep the run instant.
They restore every attribute in a `finally`.

That is a real test of the function body — not a mock of it — which is why it
catches the guards.

### 1.2 `test/test_flattrade_funds_cache.py`

A **pytest** module, hermetic by design: the broker transport is replaced with a
fake, so nothing touches Flattrade or spends the account's rate-limit budget.

| Test | Pins |
|---|---|
| `test_second_call_within_ttl_hits_the_cache_not_the_broker` | 3 requests → 1 pair of broker calls |
| `test_limits_and_positionbook_are_issued_concurrently` | peak concurrency == 2; elapsed < 0.30 s for two 150 ms calls |
| `test_concurrent_misses_collapse_into_one_broker_fetch` | 6 threads → 1 pair of calls |
| `test_transport_failure_returns_empty_dict_and_never_raises` | `WinError 10035` and an HTML body both → `{}` |
| `test_a_failure_is_not_cached` | an empty result stays retryable |
| `test_broker_error_status_returns_empty_dict` | `Not_Ok` → `{}` |
| `test_non_numeric_broker_field_returns_empty_dict` | a `null` field → `{}`, not `TypeError` |
| `test_funds_are_computed_from_both_endpoints` | the arithmetic across two positions |
| `test_different_tokens_do_not_share_a_cache_entry` | 2 tokens → 2 pairs |
| `test_cache_key_never_contains_the_raw_token` | the key is 12 chars and does not contain the token |
| `test_dead_fetch_data_signature_is_unchanged` | `fetch_data(endpoint, payload, headers, client)` is called positionally, and the timeout is `_FUNDS_TIMEOUT` |

The last one is the regression guard for the concurrency change: the
`ThreadPoolExecutor` submits `fetch_data` positionally, so a signature change
would break it silently.

### 1.3 `test/test_flattrade_protocol.py`

**Not a test.** It is a diagnostic script that probes each broker's HTTP endpoint
to report which protocol (HTTP/2 vs HTTP/1.1) it negotiates. It makes live network
calls and asserts nothing. It is listed here so nobody mistakes it for coverage.

---

## 2. What is NOT covered

Stated plainly, because a gap that is not written down is a gap that gets
forgotten:

| Area | Coverage |
|---|---|
| `transform_data` MARKET→LMT end-to-end | **none** — only `_marketable_limit_price` and `round_to_tick_size` are exercised |
| `transform_data` SL-M→SL-LMT | **none** |
| `_retention` (DAY/IOC clamp) | `test/test_flattrade_order_path_fixes.py` |
| `mpp_percentage` validation | **none** in this plugin (the shared `utils/mpp_slab.py` is exercised by other brokers' tests) |
| `cancel_all_orders_api` guards | **none** |
| `place_smartorder_api` flip clamp | **none** |
| `get_open_position` multi-row summing | **none** |
| Market-data WebSocket (routing, generations, auth retry) | **none** |
| Order-update adapter | **none** |
| Master contract processing | **none** |

The two response-shape defects that produced production HTTP 500s
(`close_all_positions`, `cancel_all_orders_api`) are covered for the first and
**not** for the second. A single parametrised test over the reply shapes would
have caught both.

---

## 3. Re-verifying without a broker

### 3.1 The square-off guards

Run the smoke test (§1.1). It needs no network and no credentials.

### 3.2 The MPP conversion

There is no test, so verify by inspection against the behaviour matrix in
`02-order-placement-mpp.md` §7. The three things to confirm in the live file:

1. `transform_data` raises `MPPPriceUnavailable` when `original_type == "MARKET"`
   and `order_type != "LMT"`.
2. `transform_data` raises when `original_type == "SL-M"` and
   `order_type != "SL-LMT"`.
3. `_reference_quote` **raises** rather than returning a sentinel.

### 3.3 The response-shape guards

Feed the shapes from `03-squareoff-safety.md` §8 and
`04-cancel-all-and-books.md` §1.3 through `_read_book` and
`cancel_all_orders_api` directly. Both are pure functions of their input once the
broker helpers are stubbed.

### 3.4 The funds path

Run `pytest test/test_flattrade_funds_cache.py`. It is hermetic.

### 3.5 The feed

No automated check exists. The observable signals in `log/`:

- **Healthy:** one `Connected to Flattrade WebSocket successfully` per session,
  and `[PUBLISH] First publish for topic: …` once per topic.
- **The generation bug returning:** repeated `Reconnecting in Ns` with a
  `WebSocket closed` immediately before each one, at a steady cadence, and
  `Ignoring callback from retired WebSocket client` **absent**.
- **The auth bug returning:** `Authentication failed` followed by no reconnect at
  all, while the adapter still reports connected.

---

## 4. The upgrade risk — read this before any OpenAlgo upgrade

`broker/flattrade/` is **vendored upstream code**, not Qwantonomous-owned. Every
modification in this folder is a **local patch**. An OpenAlgo upgrade that
overwrites these files will silently reintroduce the defects.

The two most dangerous regressions, because their symptoms are easy to
misdiagnose as broker problems:

| Regression | Symptom |
|---|---|
| `close_all_positions` loses its `_read_book` guard | Spurious "failed to square off" on a **flat** account — looks like a broker fault |
| `transform_data` loses the MPP fail-loud guards | Intermittent `0/390 Rejected` on entries and square-offs at 09:15 — looks like a broker fault |

### 4.1 The post-upgrade checklist

Run this after **any** upgrade that touches `broker/flattrade/`:

1. `api/order_api.py#_read_book` exists, and `close_all_positions` calls it
   instead of indexing `positions_response`.
2. `api/order_api.py#_is_empty_book_response` still has
   `_EMPTY_BOOK_MARKERS = ("no data",)` — a broader match would treat a session
   expiry as an empty book.
3. `api/order_api.py#cancel_all_orders_api` contains
   `isinstance(order_book_response, list)`.
4. `api/order_api.py#_get_close_lock` exists and `close_all_positions` holds it.
5. `api/order_api.py#_working_sell_orders` exists and the oversell subtraction is
   present.
6. `mapping/transform_data.py#transform_data` raises `MPPPriceUnavailable` for an
   unpriceable MARKET and an unpriceable SL-M.
7. `mapping/transform_data.py#_retention` exists and clamps to `DAY`/`IOC`.
8. `streaming/flattrade_adapter.py#_is_current` exists and every callback in
   `_build_ws_client` is wrapped.
9. `streaming/flattrade_adapter.py#_handle_auth_failure` exists and
   `max_auth_refresh_retries` is 3.
10. `api/funds.py#_account_key` hashes the token.
11. `services/order_update_service.py#_POLLING_BROKERS` still contains
    `"flattrade"`.
12. `websocket_proxy/server.py` still calls `unsubscribe_all()` for `flattrade`.

Then run `test/test_flattrade_squareoff_safeguards.py` and
`pytest test/test_flattrade_funds_cache.py`.

### 4.2 The KB record

`kb/gotchas.md` carries two `LOCAL PATCH (2026-10-22, commit …)` entries for the
square-off and cancel-all fixes. They exist **for exactly this reason** — so an
upgrade that reintroduces the defect is caught by a note rather than by a
customer.

The other modifications in this folder are **not** recorded in `kb/gotchas.md`.
That is a gap: the MPP fail-loud guards and the generation guard are at least as
consequential as the two that are recorded.

---

## 5. How to reproduce the original failures

Useful when confirming a regression is real rather than suspected.

### 5.1 The flat-account square-off (HTTP 500)

Against a pre-fix copy, call square-off on an account whose `PositionBook` read
returns a bare `{"stat": "Not_Ok", "emsg": …}`. Pre-fix, `log/errors.jsonl` gains:

```
KeyError: 0
  File "...\broker\flattrade\api\order_api.py", line 278, in close_all_positions
    if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
```

### 5.2 The cancel-all (HTTP 500)

Same account, same second. Pre-fix, `log/errors.jsonl` gains:

```
AttributeError: 'str' object has no attribute 'get'
  File "...\broker\flattrade\api\order_api.py", line 404, in cancel_all_orders_api
    if normalize_order_status(order.get("status")) == "open"
```

### 5.3 The MPP degradation

Pre-fix, a quote failure produced a `WARNING` and then a raw `MKT` in the
transformed payload. The broker answered `0/390 Rejected`. Post-fix, the same
quote failure produces `MPPPriceUnavailable` and **no order is sent**.

### 5.4 The feed loop

Pre-fix, `log/` showed ~430 `WebSocket closed` → `Reconnecting in 10s` pairs in a
single session, with the socket alive only in 5-second slices.

---

## 6. Known gaps in this documentation set

1. **No test was added as part of this work.** Everything here documents existing
   behaviour; the coverage table in §2 is the honest state, not a target.
2. **The engine-side (Dart/Flutter) contract is described from the server's side
   only.** The client lives in a different repository and was not read.
3. **The `docs/2026-10-22-flattrade-squareoff-cancelall-fixes.md` write-up at the
   repo root remains the only place with the verbatim production tracebacks.**
   This folder cites them but does not duplicate them.
