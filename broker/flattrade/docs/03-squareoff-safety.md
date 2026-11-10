# 03 — Square-off safety (`close_all_positions`)

**File:** `broker/flattrade/api/order_api.py#close_all_positions`

This is the most safety-critical function in the plugin. It is what runs when the
engine decides to exit — on a target, a stop, a trailing line, a manual exit, a
`P.Cross` side switch, or an `I.Cross` flip. It runs on retry, and it can run
concurrently.

The function was rewritten around four independent guards. Each one closes a
different hole, and removing any one of them re-opens a real production defect.

---

## 1. The engine-side contract

`services/close_position_service.py#close_position_with_auth` calls:

```python
response_code, status_code = broker_module.close_all_positions(api_key, auth_token)
```

and then:

- **200** → `{"status": "success", "message": <the broker's own message>}`.
  The broker's message is preserved verbatim, deliberately: the adapter
  distinguishes a real sweep from "nothing to close" from "a close is already
  working", and flattening those three into one sentence hides which happened.
- **Non-200** → the broker's `message` is propagated as an error.
- **Exception** → HTTP 500 `"Failed to close positions due to internal error"`.

That last line is why the plugin must never raise. An exception here loses the
reason entirely.

---

## 2. Guard 1 — read-vs-empty (`_read_book`)

### 2.1 The defect

The old guard was:

```python
if positions_response is None or positions_response[0]["stat"] == "Not_Ok":
```

It assumed the `PositionBook` reply is always a non-empty **list**. It is not.
A flat account can answer with any of:

| Reply | Old result |
|---|---|
| `None` | handled |
| `[]` | **`IndexError`** → HTTP 500 |
| `{}` | **`KeyError: 0`** → HTTP 500 |
| `{"stat": "Not_Ok", "emsg": "..."}` | **`KeyError: 0`** → HTTP 500 |
| `[{"stat": "Not_Ok", ...}]` | handled |

The production traceback (`log/errors.jsonl`, 2026-10-21 09:15:00) is
`KeyError: 0` at that exact line. It repeated at 09:15:01 and 09:15:02 — the
engine's three retries — and again the next morning.

### 2.2 The fix

`order_api.py#_read_book` normalises every shape and, crucially, **distinguishes
empty from unreadable**:

| Reply | `_read_book` result |
|---|---|
| `None` | `BrokerReadError` — "unavailable (no response from broker)" |
| `list` | the dict rows only (non-dict elements dropped) |
| `{"stat": "Not_Ok", "emsg": "... no data"}` | `[]` — genuinely empty |
| `{"stat": "Not_Ok", "emsg": <anything else>}` | `BrokerReadError` |
| `{"stat": "Ok", ...}` (single row) | `[response]` |
| anything else | `BrokerReadError` — "unexpected reply type" |

The empty-detection rule lives in `order_api.py#_is_empty_book_response` and is
deliberately narrow:

```python
_EMPTY_BOOK_MARKERS = ("no data",)
```

Only a `Not_Ok` whose `emsg` contains `"no data"` means "there are no rows".
**Every other `Not_Ok` is a genuine read failure** — Session Expired, an auth
failure, a gateway error. Collapsing those into "0" is how a close used to act on
a book it could not see.

`order_api.py#BrokerReadError` exists so the two states cannot be confused by a
caller: `[]` is a fact, an exception is an unknown.

### 2.3 Why this is a correctness fix, not a cosmetic one

A flat account is the **normal** state at the start of a session. Reporting a
failed square-off on it made the engine retry three times and held the `P.Cross`
side switch in the "closing" state — a stuck UI caused by an account that was
already exactly where the user wanted it.

---

## 3. Guard 2 — per-account serialisation (`_get_close_lock`)

### 3.1 The defect

Two sweeps reading the same stale `netqty` would each sell the whole long. The
second one takes the account **through zero into a short** — an unintended option
sale, which for a buyer is an unlimited-risk position.

### 3.2 The fix

`order_api.py#_get_close_lock` returns a `threading.Lock` keyed by the auth token,
held for the whole sweep. Unlike `_get_symbol_lock` (which is per
symbol+exchange+product, for smart orders), this one is **account-wide**, because
one sweep touches every position at once.

The lock is held across the entire function body, including the broker I/O. That
is intentional: the point is that no second sweep can read the book while the
first is deciding.

---

## 4. Guard 3 — the position cache

`order_api.py#_get_cached_positions` caches `get_positions()` for **1 second**
(`_POSITION_CACHE_TTL`), keyed by auth token. `order_api.py#_invalidate_position_cache`
drops it.

`close_all_positions` invalidates:

- **before** reading, so it never decides from a book cached before a previous
  close landed;
- **after** the sweep, so a stale snapshot never feeds the next caller;
- **after** a cancel-and-settle, before the re-read.

`place_smartorder_api` invalidates before and after every placement, for the same
reason: a book cached before the previous fill makes this order's delta too large.

The 1-second TTL is what makes the cache safe. It exists to collapse the burst of
reads a single engine tick produces, not to serve a stale view.

---

## 5. Guard 4 — the working-SELL ledger

### 5.1 The defect

A SELL left working by an **earlier** sweep was priced off a premium that has
since moved, so it may never fill. Two wrong answers were available:

- **Skip the leg** → the position stays open for ever and a retry loop can never
  converge.
- **Sell again** → the account goes through zero into a short.

### 5.2 The fix — cancel, settle, re-read, then decide

`order_api.py#_working_sell_orders` builds a ledger of SELLs still working at the
broker, keyed by `(tsym, exch, prd)` — the same raw Noren values the position
book carries, so the keys compare directly. For each:

- only `trantype == "S"`;
- only `normalize_order_status(status) == "open"`;
- `remaining = qty - fillshares`, skipped when `<= 0`;
- the **order id** is kept, because it is what has to be cancelled.

Then `close_all_positions`:

1. Collects the ids of working SELLs that would block a close for the **same
   contract**.
2. Cancels them via `order_api.py#_cancel_orders` (best-effort, per-order
   try/except, returns only the ids the broker really cancelled).
3. Sleeps `_CLOSE_CANCEL_SETTLE` (**0.15 s**) so the broker releases the
   cancelled quantity.
4. **Re-reads both books** and re-derives the ledger — never decides from the
   pre-cancel view.

### 5.3 The oversell guard

For a long position, the quantity to sell is reduced by whatever a working SELL
the broker would **not** let us cancel still covers:

```python
still_working = sum(order["remaining"] for order in working_sells.pop(key, []))
if still_working:
    quantity -= still_working
    if quantity <= 0:
        covered.append({"symbol": symbol, "working_sell_qty": still_working})
        continue
```

`pop()` is deliberate: it consumes the key, so a second row for the same contract
cannot double-count the same order.

When every open long is already covered, the function returns **200** with
`"Close already working for every open position"` and a `covered` list. That is
the honest answer — nothing was sent, and nothing more may be sent without
overselling.

### 5.4 Refusing to close blind

If the **order book** cannot be read, the function refuses to sweep at all:

```python
except BrokerReadError as error:
    logger.error(f"close_all_positions: refusing to close blind: {error}")
    return {"status": "error", "message": str(error)}, 500
```

Without a readable order book there is no way to tell whether a close is already
working, so a sweep would risk overselling. Refusing is the safe answer.

---

## 6. Honest failures

### 6.1 A rejected close is an error

The old code logged a rejected close and carried on, then returned
`"All Open Positions SquaredOff"`. The engine stopped trying and the position
stayed open.

Now each close is checked:

```python
stat = response.get("stat") if isinstance(response, dict) else None
http_status = getattr(res, "status", None)
if http_status == 200 and stat == "Ok":
    closed.append({...})
else:
    failures.append({... "message": detail or "broker rejected the close order" ...})
```

and if `failures` is non-empty the function returns **500** with a per-symbol
reason, plus the `closed` list so a partial sweep is visible.

### 6.2 An unreadable book is an error

Both the position-book read and the order-book read return **500** with the real
reason on `BrokerReadError`, and **500** with `"Position book unavailable (…)"`
on any other exception.

---

## 7. Return shapes

| Situation | Status | Payload |
|---|---|---|
| Genuinely empty book, or every row already flat | 200 | `{"message": "No Open Positions Found"}` |
| Real sweep, every close accepted | 200 | `{"status": "success", "message": "All Open Positions SquaredOff", "closed": [...]}` |
| Every open long already covered by a working SELL | 200 | `{"status": "success", "message": "Close already working for every open position", "covered": [...]}` |
| One or more closes rejected | 500 | `{"status": "error", "message": "N close order(s) failed: …", "closed": [...], "failed": [...]}` |
| Position book unreadable | 500 | `{"status": "error", "message": "<reason>"}` |
| Order book unreadable | 500 | `{"status": "error", "message": "<reason>"}` |

---

## 8. Behaviour matrix — the whole function

| Position book | Order book | Before | After |
|---|---|---|---|
| `None` | — | 200 "No Open Positions Found" | 500 (unreadable, not flat) |
| `[]` | — | **`IndexError` → 500** | 200 "No Open Positions Found" |
| `{}` | — | **`KeyError: 0` → 500** | 500 (unreadable) |
| `{"stat":"Not_Ok","emsg":"… no data"}` | — | **`KeyError: 0` → 500** | 200 "No Open Positions Found" |
| `{"stat":"Not_Ok","emsg":"Session Expired"}` | — | **`KeyError: 0` → 500** | 500 "… read failed (Session Expired)" |
| one long, no working SELL | `[]` | one SELL of `netqty` | one SELL of `netqty` |
| one long, working SELL cancellable | stale SELL | SELL placed anyway → **oversell risk** | SELL cancelled, re-read, one SELL placed |
| one long, working SELL **not** cancellable | still working | SELL placed anyway → **oversell risk** | no SELL, 200 `covered` |
| one long, close rejected | `[]` | **200 "SquaredOff"** (false success) | **500** with the reason |
| two concurrent sweeps | `[]` | both sell → **short** | serialised, one sells |

---

## 9. Known gaps (stated, not hidden)

1. **The post-cancel re-read only catches `BrokerReadError`.** A network
   exception during that re-read propagates to the service layer and becomes the
   generic HTTP 500 "Failed to close positions due to internal error". The reason
   survives only in `log/errors.jsonl`. The first two reads do catch the broad
   `Exception`; this third one does not.
2. **`_CLOSE_CANCEL_SETTLE` is a fixed 0.15 s.** It is a guess at how long
   Flattrade needs to release a cancelled quantity. A slower broker day could
   make the re-read see the order still working, which routes into the `covered`
   branch — safe, but it means the sweep does nothing that cycle.
3. **The lock is per-process.** Two server processes (e.g. a gunicorn worker
   pool) would each hold their own lock. The 1-second cache and the working-SELL
   ledger still bound the damage, but the serialisation is not cross-process.
4. **`covered` is reported as success.** That is correct for "do not oversell",
   but a caller that only checks `status == "success"` cannot tell a real sweep
   from a no-op. The `message` distinguishes them; the status does not.
