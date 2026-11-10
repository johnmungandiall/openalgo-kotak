# 01 — Engine requirements, and the gap analysis behind this work

The Qwantonomous Engine is a strategy client. It does not talk to Flattrade
directly: it talks to the OpenAlgo server, which loads a **broker plugin** for
whichever broker the user connected. `broker/flattrade/` is that plugin.

This document states what the engine needs from a plugin, then records — for
each requirement — what the plugin did before and what it does now.

---

## 1. The requirement set

The engine's own behaviour imposes seven hard requirements on a broker plugin.
They are not stylistic; each one, when unmet, produces a user-visible trading
defect.

### R1 — A MARKET order must actually reach the exchange

The engine places `MARKET` for entries and for square-off. It treats an accepted
order as "the position moved". If the broker silently rejects the price type,
the engine believes it is in a position it does not hold.

### R2 — A square-off must never take the account through zero

The engine's exit paths (`squareOffAll`, the `P.Cross` side switch, the
`I.Cross` flip) call square-off repeatedly and on retry. Two sweeps reading the
same stale quantity would sell the same long twice — the second one opens a
**short**. For an options buyer that is an unlimited-risk position the user
never asked for.

### R3 — "Nothing to do" must not look like a failure

A flat account is the normal state at the start of a session. If square-off or
cancel-all reports HTTP 500 on a flat account, the engine retries (3 attempts on
every exit path) and holds its side switch in the "closing" state.

### R4 — A failure must not look like success

The inverse of R3, and more dangerous. If the plugin reports
`"All Open Positions SquaredOff"` while placing no orders, the engine stops
trying and the position stays open.

### R5 — A price feed must survive a drop without lying about it

The engine's PnL, trailing stops and exit lines ride on the market-data
WebSocket. A socket that is "connected" but silent is worse than a closed one:
the engine keeps computing against a frozen price.

### R6 — A balance read must not cost two broker round-trips per poll

The dashboard polls funds several times a second. Flattrade's account-wide rate
limit is shared with order placement. A funds path that spends the quota is a
funds path that starves the order path.

### R7 — A credential must never appear in a log or a cache key

The session token is a live trading credential. It is re-issued on every login.

---

## 2. Gap analysis — requirement by requirement

### R1 — MARKET orders

**Before.** `mapping/transform_data.py#transform_data` mapped `MARKET → MKT` and
`SL-M → SL-MKT` and sent them. Noren rejects both for API orders. The failure
was intermittent because it depended on whether the MPP branch happened to run:
when the quote fetch failed, the code logged a warning and **fell through to the
raw `MKT`**, which the OMS then rejected with `0/390 Rejected`.

**After.** Every MARKET is rewritten to a marketable `LMT`, every SL-M to
`SL-LMT`. If a price cannot be derived, the order is **refused** with
`MPPPriceUnavailable` rather than sent invalid. Full detail in
`02-order-placement-mpp.md`.

### R2 — Oversell protection

**Before.** `api/order_api.py#close_all_positions` read the position book, then
looped and sold `abs(netqty)` per row. Nothing serialised two concurrent sweeps
and nothing accounted for a SELL that was already working at the broker.

**After.** Three independent guards: a per-account lock, a 1-second position
cache that is invalidated before and after every sweep, and a working-SELL
ledger that subtracts what the broker already has working. Detail in
`03-squareoff-safety.md`.

### R3 — Flat account must be a 200

**Before.** `if positions_response is None or positions_response[0]["stat"] == "Not_Ok"`
— indexing a reply that is not always a list. A flat account answering with a
bare `{"stat":"Not_Ok"}` dict raised `KeyError: 0`; an empty list raised
`IndexError`. `services/close_position_service.py#close_position_with_auth`
catches everything and returns HTTP 500.

**After.** `api/order_api.py#_read_book` normalises every reply shape and
distinguishes **empty** from **unreadable**. Empty → 200 "No Open Positions
Found". Unreadable → 500 with the real reason. Detail in `03-squareoff-safety.md`.

### R4 — No false success

**Before.** Two separate false-success paths existed. A rejected close order was
logged and then ignored, so the function still returned
`"All Open Positions SquaredOff"`. And an unreadable book could be treated as
flat.

**After.** A rejected close is collected into a `failures` list and the function
returns **500** with the per-symbol reason. An unreadable book raises
`BrokerReadError` and returns 500. The only 200s are: a real sweep, a genuinely
empty book, and "a close is already working for every open position" (which is
reported as its own message, not as a sweep).

### R5 — Feed honesty

**Before.** Three defects compounded:

1. The market-data cache and every routing index were keyed by the **bare
   token**. Noren tokens are unique only within an exchange, so two instruments
   merged into one slot and one symbol's price was published under the other's
   topic (issue #1732).
2. A retired socket's `on_close` could still fire — synchronously inside
   `stop()`, and again from its own reader thread — and schedule a reconnect
   that tore down the **healthy** replacement. That feedback loop produced 430
   self-inflicted closes in one trading session, with the socket alive only in
   5-second slices.
3. An auth rejection left the socket open and silent, so the adapter reported
   `connected` while subscribed to nothing.

**After.** Scrip-keyed routing (`"NFO|65872"`), a client-generation guard that
drops callbacks from retired sockets, and a separate tighter-bounded auth-retry
path that force-closes a rejected socket. Detail in `06-market-data-websocket.md`.

### R6 — Funds cost

**Before.** `api/funds.py#get_margin_data` issued `/PiConnectAPI/Limits` and
then `/PiConnectAPI/PositionBook` **serially**, with no cache anywhere on the
path, and it bypassed the account-wide rate limiter entirely. The busiest caller
in the process was the only one not counted against the quota.

**After.** The two calls are issued concurrently, the processed result is served
from a 1-second TTL cache, concurrent misses collapse into a single broker fetch
(single-flight), both calls take a rate-limiter slot, and each carries a 5-second
per-request timeout instead of inheriting the shared client's 120 s. Detail in
`05-funds-and-rate-limits.md`.

### R7 — Credential hygiene

**Before.** The funds cache did not exist, so there was no cache key to leak.
The risk arrived with the cache.

**After.** `api/funds.py#_account_key` hashes the token with SHA-256 and keeps
only the first 12 hex characters. The raw token never becomes a key, so it can
never reach a debug log line. The same rule is applied in
`api/order_api.py#_get_close_lock`, which keys on the token but never logs it.

---

## 3. What the engine-side contract actually is

The plugin does not define its own return shapes freely — the service layer
above it does, and the plugin must match. These are the contracts the
modifications were written against, read from the live tree:

| Service | Calls | Expects |
|---|---|---|
| `services/close_position_service.py#close_position_with_auth` | `broker_module.close_all_positions(api_key, auth_token)` | `(payload, status_code)`. **200** → success, and the broker's own `message` is preserved verbatim. **Non-200** → the broker's `message` is propagated as an error. **Exception** → HTTP 500 "Failed to close positions due to internal error". |
| `services/cancel_all_order_service.py#cancel_all_orders_with_auth` | `broker_module.cancel_all_orders_api(order_data, auth_token)` | `(canceled_orders, failed_cancellations)`. **Any exception** → HTTP 500 "Failed to cancel all orders due to internal error". |
| `services/place_order_service.py` | `broker_module.place_order_api(data, auth)` | `(res, response_data, orderid)`; `res.status` is read for the HTTP code. |
| `services/place_smart_order_service.py` | `broker_module.place_smartorder_api(data, auth)` | Same triple. |
| `services/modify_order_service.py` | `broker_module.modify_order(data, auth)` | `(payload, status_code)`. |
| `services/funds_service.py` | `broker_module.get_margin_data(auth_token)` | A dict of the five display fields; `{}` means "no funds available here". |
| `websocket_proxy/server.py` | `adapter.unsubscribe_all()` | Called instead of `disconnect()` for `flattrade` and `shoonya` when the last client leaves. |

Two consequences worth stating plainly:

- **The service layer trusts the plugin's return values blindly.** That is why
  the plugin-level return shapes matter so much, and why "return the broker's
  documented no-op answer" is a correctness fix rather than a cosmetic one.
- **A plugin exception is never a plugin problem.** It becomes an HTTP 500 with
  a generic message, and the real cause survives only in `log/errors.jsonl`.

---

## 4. The one rule that generalises

Every defect in this folder is the same defect wearing a different name:

> **A broker helper's reply is not always the documented success payload.**
> On failure it is the broker's error object — a bare dict, a string, `None`.
> Indexing or iterating it before type-checking it raises, and the service layer
> reports that `TypeError` as a failed trading operation.

The corollary, which is what the fixes actually implement:

> **Distinguish "empty" from "unreadable" explicitly, and never let one stand in
> for the other.** An empty book is a fact. An unreadable book is an unknown.
> A trading decision may be made on the first and must never be made on the
> second.
