# 06 — Market-data WebSocket: routing, generations, auth retry

**Files:** `broker/flattrade/streaming/flattrade_adapter.py`,
`broker/flattrade/streaming/flattrade_websocket.py`,
`broker/flattrade/streaming/flattrade_mapping.py`

The engine's PnL, trailing stops and exit lines all ride on this feed. A socket
that is "connected" but silent is worse than a closed one: the engine keeps
computing against a frozen price. Three separate defects had to be fixed before
the feed could be trusted.

---

## 1. Defect 1 — token-keyed routing (issue #1732)

### 1.1 The problem

Noren tokens are unique only **within an exchange**. The live master contract
carries thousands of cross-exchange duplicates (NSE/CDS, BSE_INDEX/NSE, BSE/MCX).

The old adapter keyed its market-data cache and every routing index by the **bare
token**. Two different instruments therefore merged into one cache slot, and one
symbol's price was published under the other's topic. The user saw a chart jump
to a price that belonged to a different contract.

### 1.2 The fix — key everything by scrip

A **scrip** is `"<exchange>|<token>"`, e.g. `"NFO|65872"`. Every routing structure
is keyed by it:

| Structure | Purpose |
|---|---|
| `MarketDataCache._cache` | merged tick data per scrip |
| `MarketDataCache._initialized_scrips` | which scrips have logged their first tick |
| `subscriptions` | `correlation_id → subscription` |
| `scrip_to_symbol` | `scrip → (symbol, exchange)` |
| `ws_subscription_refs` | `scrip → {touchline_count, depth_count}` |
| `_token_to_scrips` | `token → set of scrips` (fallback index only) |

`flattrade_adapter.py#_create_subscription` builds the scrip and **validates the
exchange mapping** before using it:

```python
flattrade_exchange = FlattradeExchangeMapper.to_flattrade_exchange(brexchange)
if not flattrade_exchange:
    raise ValueError(f"Unsupported exchange: {brexchange}")
scrip = f"{flattrade_exchange}|{token}"
```

Without that check an unmapped exchange produces `"None|65872"`, which would
never match the exchange on an incoming feed message — a subscription that
silently receives nothing.

### 1.3 The fallback, and why it is narrow

`flattrade_adapter.py#_resolve_scrip` maps a feed message's `(exchange, token)`
pair to a subscribed scrip. The exchange comes straight from the message's `e`
field, which Flattrade sends on every `tf`/`tk`/`df`/`dk` packet.

When `e` is missing, it falls back to the token index — **but only when that
resolves unambiguously**:

```python
candidates = self._token_to_scrips.get(token)
if not candidates:
    return None
if len(candidates) > 1:
    self.logger.warning(
        f"Feed message for token {token} has no exchange and matches "
        f"{len(candidates)} subscribed scrips ({sorted(candidates)}); dropping "
        f"rather than routing it to the wrong symbol"
    )
    return None
return next(iter(candidates))
```

Dropping an ambiguous message is the correct answer. Mis-routing it is exactly
what issue #1732 was about.

---

## 2. Defect 2 — the reconnect feedback loop

### 2.1 The problem

A socket the adapter retires can still deliver `on_close` / `on_error` from its
own thread — and `stop()` delivers them **synchronously**, while the replacement
is being built.

Routed in unfiltered, such a callback:

1. sets `connected = False` on a **healthy** connection;
2. calls `_schedule_reconnection()`;
3. whose timer tears down the **healthy** replacement;
4. whose retirement fires another `on_close`;
5. and so on.

That feedback loop turned a single drop into a permanent ~10 s kill-and-reconnect
cycle: **430 self-inflicted closes in one trading session**, with the market-data
socket alive only in 5-second slices. That is the customer's "chart freezes then
jumps".

### 2.2 The fix — a client generation counter

`flattrade_adapter.py#_setup_connection_management` adds
`self._client_generation = 0`. It is incremented every time the current client is
**retired** (reconnect or shutdown).

`flattrade_adapter.py#_build_ws_client` captures the generation and wraps **every**
callback:

```python
generation = self._client_generation
return FlattradeWebSocket(
    ...
    on_message=lambda ws, msg: (self._on_message(ws, msg) if self._is_current(generation) else None),
    on_error=lambda ws, err: (self._on_error(ws, err) if self._is_current(generation) else None),
    on_close=lambda ws, code, msg: (self._on_close(ws, code, msg) if self._is_current(generation) else None),
    on_open=lambda ws: (self._on_open(ws) if self._is_current(generation) else None),
)
```

`flattrade_adapter.py#_is_current` returns `True` only while the generation is
still live, and logs at `debug` when it drops a callback.

### 2.3 The ordering that matters

`flattrade_adapter.py#_retire_client` bumps the generation **before** `stop()`:

```python
self._client_generation += 1
client = self.ws_client
self.ws_client = None
if not client:
    return
try:
    client.stop()
except Exception as cleanup_err:
    self.logger.warning(f"Error cleaning up old WebSocket: {cleanup_err}")
```

Doing it in the other order would let `stop()`'s synchronous `on_close` schedule a
reconnect for the client we are about to replace. The code comment states this
explicitly.

---

## 3. Defect 3 — an auth rejection left the socket open and silent

### 3.1 The problem

Flattrade does **not** proactively close the socket on an auth rejection. It just
sits open, silently rejected. The adapter would report `connected` while
subscribed to nothing and receiving nothing — forever.

### 3.2 The fix — two layers

**Layer 1, in the client.** `flattrade_websocket.py#_handle_auth_response` sets
`auth_failed` / `auth_failure_message` and force-closes:

```python
else:
    self.connected = False
    self.logger.error(f"Authentication failed: {data}")
    self.auth_failed = True
    self.auth_failure_message = str(data.get("emsg") or data.get("s") or "unknown reason")
    self._close_websocket()
```

`flattrade_websocket.py#_on_error` also sets the flag when the payload looks like
an auth failure, via `_is_fatal_auth_error` and a deliberately tight indicator
list:

```python
_AUTH_FAILURE_INDICATORS = (
    "401", "403", "unauthorized", "forbidden",
    "invalid session", "invalid token", "session expired",
)
```

Kept tight to avoid false positives on transient network errors — those **should**
be retried.

**Layer 2, in the adapter.** `flattrade_adapter.py#_on_close` and
`_handle_websocket_error` route to `_handle_auth_failure` instead of the generic
reconnect when `ws_client.auth_failed` is set.

### 3.3 Why a separate retry budget

`flattrade_adapter.py#_handle_auth_failure` uses its own counter, deliberately
**separate from and much tighter-bounded than** `reconnect_attempts` /
`MAX_RECONNECT_ATTEMPTS`:

```python
self.auth_refresh_retries = 0
self.max_auth_refresh_retries = 3
```

The reasoning, from the code comment: Flattrade tokens roll over daily at 5–6 AM
IST, so an auth failure needs a few chances to self-heal via a fresh DB read —
but it must stop well short of the full 50-attempt generic backoff, so a genuinely
dead token is not hammered for many minutes against a possibly rate-limited IP
(SEBI static-IP mandate, effective April 1, 2026).

It also resets `reconnect_attempts = 0` before scheduling, so an auth retry does
not inherit a long exponential delay meant for network blips:

```python
# Don't inherit a long exponential delay meant for network blips -
# an auth retry should attempt again promptly.
self.reconnect_attempts = 0
```

### 3.4 The token refresh on reconnect

`flattrade_adapter.py#_attempt_reconnection` re-reads the token from the database
before rebuilding the client:

```python
fresh_token = get_auth_token(self.user_id, bypass_cache=True)
if fresh_token:
    self.accesstoken = fresh_token
else:
    self.logger.warning("Could not fetch fresh auth token on reconnect; using existing token")
```

Reusing the construction-time token would reconnect with a dead token after the
daily rollover.

---

## 4. Reconnect timing

`flattrade_adapter.py#Config`:

| Constant | Value | Note |
|---|---|---|
| `MAX_RECONNECT_ATTEMPTS` | 50 | |
| `BASE_RECONNECT_DELAY` | **2 s** | was 5 s |
| `MAX_RECONNECT_DELAY` | 60 s | |
| `CACHE_COMPLETENESS_THRESHOLD` | 0.3 | |
| `WEBSOCKET_TIMEOUT` | 30 s | |

The base delay was halved deliberately: every drop is a blind window on the
customer's chart, and the peer is a single broker endpoint — not a shared herd —
so a tight first retry costs nothing and halves the freeze.

`flattrade_adapter.py#_schedule_reconnection` computes
`min(BASE * 2**attempts, MAX)` and stores the timer so `disconnect()` can cancel
it. Both the reconnect timer and the batch-subscription timer are `daemon=True`
so they cannot block process exit.

---

## 5. Batch subscription

Rapid `subscribe()` calls are coalesced into a single touchline and a single depth
message, to avoid hammering the socket.

- `_queue_subscription` appends to `subscription_queue` and starts the timer on
  the **first** item.
- `_start_batch_timer` (re)starts a `threading.Timer` with `batch_delay = 0.5 s`.
- `_process_batch_subscriptions` de-duplicates while preserving order, clears the
  queue, **snapshots the client and releases the lock before network I/O**, then
  sends `"#".join(scrips)` per type.

If the socket is not connected at flush time, the batch is skipped with a warning
and `_resubscribe_all()` rebuilds from `self.subscriptions` on reconnect — so
nothing is lost.

`_on_close` and `unsubscribe_all` both cancel the timer and clear the queue,
because queued items would subscribe to scrips that are no longer wanted.

---

## 6. Reference counting

`ws_subscription_refs[scrip] = {"touchline_count": N, "depth_count": M}`.

- The **first** reference (0 → 1) queues the subscribe.
- Later references only bump the counter, so no redundant subscribe is sent.
- `unsubscribe` decrements; only when the count reaches 0 is the broker told.

This is what lets several engine components subscribe to the same symbol without
each opening a broker subscription — and what stops one component's unsubscribe
from killing another's feed.

---

## 7. The persistent-session model

Flattrade imposes a **server-side session cooldown**: when a WebSocket is cleanly
closed, the server begins tearing down the session, and a new connection with the
same token during that window is rejected. This is why restarting the app used to
"fix" a reconnect failure — it gave the server time to finish.

`flattrade_adapter.py#unsubscribe_all` sidesteps it entirely: it sends unsubscribe
requests for every active stream but **does not close the socket**. The adapter
stays connected and authenticated, ready for the next client.

`websocket_proxy/server.py` calls it instead of `disconnect()` for `flattrade` and
`shoonya` when the last client leaves:

```python
if broker_name in ["flattrade", "shoonya"] and hasattr(adapter, "unsubscribe_all"):
```

Benefits: no cooldown window, no repeated TCP/TLS/WS handshakes, and instant data
for the next client.

`unsubscribe_all` also cancels the batch timer and clears the queue, then clears
`subscriptions`, `scrip_to_symbol`, `_token_to_scrips`, `ws_subscription_refs` and
the market cache — but leaves the connection alive.

---

## 8. Data normalisation

Three normalisers, selected by mode:

| Mode | Class | Fields |
|---|---|---|
| 1 (LTP) | `LTPNormalizer` | `ltp`, `flattrade_timestamp` |
| 2 (Quote) | `QuoteNormalizer` | + `volume`, `open`, `high`, `low`, `close`, `average_price`, `percent_change`, `last_quantity`, `last_trade_time` |
| 3 (Depth) | `DepthNormalizer` | + `total_buy_quantity`, `total_sell_quantity`, and (for `df`/`dk`) a 5-level `depth` with `upper_circuit`, `lower_circuit`, `52_week_high`, `52_week_low`, `total_traded_value` |

All numeric conversion goes through `safe_float` / `safe_int`, which treat
`None`, `""` and `"-"` as the default and swallow `ValueError` / `TypeError`.
Noren sends `"-"` for an absent number, which is why that case is explicit.

### The cache merge

`MarketDataCache._merge_data` handles partial updates. Noren sends `tk` (partial
touchline) as well as `tf` (full), so a partial packet must not blank fields the
full packet already populated.

- `_should_preserve_cached_value` keeps a non-zero cached OHLC when the new value
  is zero (`o`, `h`, `l`, `c`, `ap`).
- `_preserve_missing_fields` copies any cached field absent from the new packet.

`_is_zero_value` treats `None`, `""`, `"0"`, `0`, `"0.0"`, `0.0` as zero.

---

## 9. Capabilities

`flattrade_mapping.py#FlattradeCapabilityRegistry`:

| Capability | Value |
|---|---|
| Supported modes | `{1, 2, 3}` |
| Supported depth levels | `{5}` — Flattrade offers only 5-level depth |
| Max subscriptions | 5000 |
| Max instruments per request | 50 |

`flattrade_mapping.py#FlattradeExchangeMapper` maps `NSE_INDEX → NSE` and
`BSE_INDEX → BSE`, because indices stream on the base exchange.

---

## 10. Known gaps (stated, not hidden)

1. **`_resubscribe_all` calls `self.ws_client.subscribe_touchline(...)` without a
   null check.** It is reached from `_on_open`, which only fires for a live
   client, so in practice the client exists — but a race with `disconnect()`
   would raise `AttributeError` inside a callback.
2. **`_resubscribe_all` holds `self.lock` across the network sends.** The batch
   flush deliberately releases the lock before I/O; this path does not.
3. **`MarketDataCache` has no size ceiling.** It is cleared per scrip on
   unsubscribe and entirely on disconnect, so it is bounded by the subscription
   count — but a long session with heavy subscribe churn grows it.
4. **`_published_topics` is created lazily with `hasattr`** and never cleared, so
   the "first publish" log line is once per process, not once per connection.
