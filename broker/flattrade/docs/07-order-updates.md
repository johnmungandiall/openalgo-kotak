# 07 — Order updates: the adapter, and why Flattrade polls instead

**Files:** `broker/flattrade/streaming/flattrade_order_adapter.py`,
`services/order_update_service.py`, `websocket_proxy/order_adapter.py`

The engine needs to know when an order fills, is rejected, or is cancelled. This
document covers the adapter that was written for Flattrade — and the reason it is
**not** the path Flattrade actually uses.

---

## 1. The decision: Flattrade polls

`services/order_update_service.py#_POLLING_BROKERS` is
`{"groww", "fivepaisa", "samco", "flattrade"}`.

The reason, from the code comment:

> Flattrade's PiConnectWSAPI permits only **ONE** WebSocket connection per user
> account (uid). When a dedicated order-update socket connects, Flattrade evicts
> the market-data socket (opcode 8 / 1000 close frame), and reconnecting market
> data evicts the order-update socket in an endless loop. Switching to REST
> polling keeps market data connected uninterrupted.

This is the correct trade. Market data is the feed the engine's PnL and exits
ride on; order updates are a convenience. Losing the price feed to gain push
order updates would be a bad trade, and the two cannot coexist.

`_build_adapter` therefore returns a
`websocket_proxy/order_adapter.py#PollingOrderUpdateAdapter` for Flattrade, and
the dedicated adapter below is **not instantiated** on the normal path.

### Why multiplexing onto the market-data socket was not an option

The market-data adapter runs in the `websocket_proxy` **subprocess** (under
gunicorn+eventlet and Docker). An `OrderUpdateEvent` published there would land on
the wrong process's event bus. The same reasoning rules out the approach for
5Paisa.

---

## 2. The polling adapter

`websocket_proxy/order_adapter.py#PollingOrderUpdateAdapter`:

- Polls `services.orderbook_service.get_orderbook` every
  `ORDER_POLL_INTERVAL` seconds (default **5**).
- Diffs per `orderid` on `(order_status, filled_quantity)`.
- Publishes an `OrderUpdateEvent` for each **change**.
- **Seeds the baseline silently** on the first successful poll, so a restart does
  not replay the whole book as fresh updates.

The baseline seed is the important detail. Without it, every restart would emit an
event for every order in the book, and any consumer that reacts to "order
complete" would fire on orders that completed hours ago.

It exposes the same `connect()` / `disconnect()` / `connected` surface as
`BaseOrderUpdateAdapter`, so `order_update_service.py` manages both uniformly.

---

## 3. The dedicated adapter (present, not active)

`broker/flattrade/streaming/flattrade_order_adapter.py` is a complete, correct
implementation of Flattrade's order-update protocol. It is kept because:

- it documents the protocol precisely, which is what a future multiplexed
  implementation would need;
- it is the fallback if Flattrade ever lifts the one-connection limit;
- it is registered in `_BROKER_FACTORIES`, so removing it would break the
  registry.

### 3.1 The Noren handshake

Endpoint: `wss://piconnect.flattrade.in/PiConnectWSAPI/`

| Step | Frame |
|---|---|
| 1 | connect, send `{"t":"a","uid":…,"actid":…,"source":"API","accesstoken":…}` |
| 2 | wait for `{"t":"ak","s":"OK"}` ack |
| 3 | send `{"t":"o","actid":…}` to subscribe order updates |
| 4 | order feed arrives as `{"t":"om", …Noren order fields…}` |
| 5 | send `{"t":"h"}` heartbeats every 30 s (acked as `t="hk"`) |

### 3.2 Two Flattrade-specific quirks

1. **The order feed carries the product in `pcode`, not `prd`.** Both are read:
   `product_code = data.get("pcode") or data.get("prd") or ""`.
2. **There is NO subscription acknowledgement** for the order-update subscribe.
   The `t="o"` frame is fire-and-forget, so nothing waits on an ack. Unsubscribe
   is `t="uo"` (acked `"uok"`), not the depth `"ud"`.

### 3.3 The connect-ack watchdog

`flattrade_order_adapter.py#_close_after_failed_ack` exists because a rejected
Noren auth does not always drop the socket — Flattrade can hold it open, which
would leave the adapter reporting `connected` while subscribed to nothing and
silent forever.

Closing it makes `run_forever` return, so `BaseOrderUpdateAdapter`'s backoff loop
retries with a fresh handshake.

The ack check accepts either casing, because `flattrade_websocket.py` compares
`s == "OK"` while the doc writes `"Ok"`:

```python
if _noren_text(data.get("s") or data.get("stat")) != "ok":
```

The auth verdict and the socket check are kept **separate**, so the log says which
one failed.

### 3.4 Status mapping — and the deliberate divergence

`flattrade_order_adapter.py#_STATUS_MAP` folds every in-flight OMS state to
`"open"`:

| Noren | OpenAlgo |
|---|---|
| `complete`, `executed` | `complete` |
| `open`, `new`, `replaced`, `pending`, `open pending`, `modify pending`, `cancel pending`, `after market order req received` | `open` |
| `trigger pending` | **`trigger pending`** |
| `rejected`, `reject` | `rejected` |
| `canceled`, `cancelled` | `cancelled` |

**`trigger pending` is NOT folded into `open` here**, unlike
`mapping/order_data.py#normalize_order_status`. That is intentional: this is the
push-event path, whose consumers (`services/flow_order_update_monitor_service.py`'s
watch filters, the WS clients) expect the distinct value. The REST orderbook path
folds it because the React order book gates Modify/Cancel on
`order_status === 'open'`.

The code comment says: *"Don't unify them."*

### 3.5 The `reporttype` fallback

Noren sometimes carries a terminal state only in `reporttype` ("Order event that
triggered this message (Fill, Rejected, Canceled)"). It is consulted **only when
`status` is unmapped**:

```python
_REPORTTYPE_FALLBACK = {"rejected": "rejected", "canceled": "cancelled", "cancelled": "cancelled"}
```

**`Fill` is deliberately absent.** It rides along with partial fills too, so it
cannot mean "complete".

### 3.6 Field mapping

| OpenAlgo field | Noren source |
|---|---|
| `orderid` | `norenordno` |
| `symbol` | `to_openalgo_symbol(tsym, exch)` |
| `exchange` | `exch` |
| `action` | `trantype` via `{B: BUY, S: SELL}` |
| `quantity` | `qty` |
| `price` | `prc` |
| `trigger_price` | `trgprc` |
| `pricetype` | `prctyp` via `{LMT: LIMIT, MKT: MARKET, SL-LMT: SL, SL-MKT: SL-M}` |
| `product` | `pcode`/`prd` via `{C: CNC, M: NRML, I: MIS}` |
| `order_status` | mapped status |
| `filled_quantity` | `fillshares` |
| `pending_quantity` | `max(qty - fillshares, 0)` |
| `average_price` | `avgprc` |
| `rejection_reason` | `rejreason`, only when `order_status == "rejected"` |

`_noren_text` lowercases and folds `_` to a space, so `TRIGGER_PENDING` and
`TRIGGER PENDING` (both appear across Noren deployments) compare equal.

### 3.7 Credentials

`flattrade_order_adapter.py#create_flattrade_order_adapter`:

- `accesstoken = get_auth_token(user_id, bypass_cache=True)` — Flattrade's jKey
  session token.
- `flattrade_uid` = the **first** component of `BROKER_API_KEY`, which is always
  `"userid:::api_key"` for Flattrade.

Note the asymmetry with `api/auth_api.py#authenticate_broker`, which takes
component **[1]** (the API key) unconditionally — login cannot work without the
`":::"`. The uid/actid resolution matches
`streaming/flattrade_adapter.py` and `api/order_api.py`.

---

## 4. The base class contract

`websocket_proxy/order_adapter.py#BaseOrderUpdateAdapter` runs **in the Flask/
gunicorn process** as a background thread — unlike market-data adapters, which run
out-of-process under the `websocket_proxy` subprocess.

Subclasses implement `get_ws_url()`, `get_headers()`, `normalize()`, and may
override `on_open_extra()`, `heartbeat_interval()`, `send_heartbeat()`,
`ws_ping_interval()`.

Reconnect backoff: `_RECONNECT_BACKOFFS = [1, 2, 5, 10, 30, 60]`, capped.

### The auth-rejection stand-down

`_is_auth_rejection` returns `True` for a 401/403. A 401/403 says the credential is
dead, not that the network hiccuped, so reconnecting cannot fix it — every retry
is another rejected handshake and another ERROR line. The adapter stands down and
waits for a login.

### The heartbeat generation guard

`_start_heartbeat_thread` binds the thread to the `ws` it was started for:

```python
ws_at_start = self._ws
def loop():
    while self._running and self._ws is ws_at_start:
        ...
```

A reconnect replaces `self._ws` and spawns a fresh heartbeat thread via `on_open`,
so the previous thread exits instead of leaking one thread per reconnect.

---

## 5. Known gaps (stated, not hidden)

1. **The dedicated adapter is dead code on the normal path.** It is correct and
   tested by inspection, but no Flattrade user reaches it. That is a deliberate
   trade (see §1), not an oversight — but it means a change to it cannot be
   validated against live traffic.
2. **Polling costs a broker round-trip every 5 s per user.** It is counted against
   the account-wide rate limit through `get_orderbook`, which is correct but not
   free.
3. **A 5-second poll can miss a transient state.** An order that goes
   `open → complete` between two polls is reported once, as `complete`. That is
   the right outcome for the engine, but a consumer that wanted to observe the
   intermediate state cannot.
4. **`_POLLING_BROKERS` is a hardcoded set.** Adding a broker means editing the
   service, not the plugin. That is consistent with how the rest of the registry
   works, but it is a coupling worth knowing about.
