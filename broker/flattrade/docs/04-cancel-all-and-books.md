# 04 — Cancel-all, book mapping, and the smart-order flip clamp

**Files:** `broker/flattrade/api/order_api.py`,
`broker/flattrade/mapping/order_data.py`

Square-off (document 03) is the dangerous path. This document covers the three
functions that sit beside it: cancel-all, the book mappers, and the smart-order
delta calculation.

---

## 1. `cancel_all_orders_api` — the response-shape guard

### 1.1 The defect

The engine's exit sequence is `cancelAllOrders` → 200 ms settle → `squareOffAll`.
Cancel-all runs first so a resting order cannot fight the square-off.

The old code:

```python
if order_book_response is None:
    return [], []
orders_to_cancel = [
    order for order in order_book_response
    if normalize_order_status(order.get("status")) == "open"
]
```

A **failed** `OrderBook` read does not return a list — it returns a bare
`{"stat": "Not_Ok", "emsg": ...}` **dict**. Iterating a dict yields its **string
keys**, so `order` became the string `"stat"`, and `"stat".get(...)` raised:

```
AttributeError: 'str' object has no attribute 'get'
```

`services/cancel_all_order_service.py#cancel_all_orders_with_auth` turns any
exception into HTTP 500 `"Failed to cancel all orders due to internal error"` —
even with nothing pending to cancel.

Client-side this was **non-fatal**: the engine logged it and carried on to square
off. So it was noise rather than a blocked operation — but it poisoned the error
log and made a healthy account look broken.

### 1.2 The fix

Two independent guards, each closing a different hole:

```python
if not isinstance(order_book_response, list):
    return [], []

orders_to_cancel = [
    order
    for order in order_book_response
    if isinstance(order, dict)
    and normalize_order_status(order.get("status")) == "open"
]
```

- `isinstance(order_book_response, list)` — the reply itself is not a list (the
  reported bug).
- `isinstance(order, dict)` per item — the list contains a non-dict element
  (defensive, same family; the `norenordno` / `status` reads below assume dicts).

The return shape `(canceled_orders, failed_cancellations)` was **not** changed,
so the service layer needed no edit and its success message
(`"Canceled N orders. Failed to cancel M orders."`) is unchanged.

### 1.3 Behaviour matrix

| `get_order_book()` reply | Before | After |
|---|---|---|
| `None` | `([], [])` | `([], [])` |
| `{"stat":"Not_Ok","emsg":"…"}` | **`AttributeError` → HTTP 500** | `([], [])`, no crash |
| `[]` | `([], [])` | `([], [])` |
| `["some-string"]` | **`AttributeError`** | `([], [])` |
| one OPEN order `O1` | `(['O1'], [])` | `(['O1'], [])` |
| one OPEN + one COMPLETE | cancels only the OPEN one | cancels only the OPEN one |

---

## 2. `normalize_order_status` — and the deliberate divergence

`mapping/order_data.py#normalize_order_status` maps a raw Noren status to
OpenAlgo's four-value vocabulary. Noren arrives with underscores and inconsistent
casing (`TRIGGER_PENDING`, `Open`, `OPEN`, `REJECT`, `REJECTED`), so the function
normalises the separator and case first:

```python
status = str(raw_status or "").strip().upper().replace("_", " ")
```

| Noren | OpenAlgo |
|---|---|
| `COMPLETE` | `complete` |
| `OPEN`, `PENDING`, `TRIGGER PENDING`, `NEW`, `REPLACED`, `OPEN PENDING`, `MODIFY PENDING`, `CANCEL PENDING`, `AFTER MARKET ORDER REQ RECEIVED` | `open` |
| `REJECTED`, `REJECT` | `rejected` |
| `CANCELED`, `CANCELLED` | `cancelled` |
| anything else | lowercased as-is |

`TRIGGER_PENDING` folding into `open` is deliberate: this is the **REST
orderbook** path, where the React order book gates its Modify/Cancel buttons on
`order_status === 'open'`. A stop-loss waiting for its trigger must offer those
buttons.

### The divergence you must not "fix"

`streaming/flattrade_order_adapter.py#_STATUS_MAP` maps `trigger pending` to
**`"trigger pending"`**, not to `open`. That is the **push-event** path, whose
consumers (`services/flow_order_update_monitor_service.py`'s watch filters, the
WS clients) expect the distinct value.

The two maps are intentionally different. The code comment says so explicitly:
*"Don't unify them."*

---

## 3. `map_order_data` — the order book mapper

`mapping/order_data.py#map_order_data` rewrites raw Noren rows into OpenAlgo
shape. Three things in it are worth knowing:

### 3.1 Product mapping runs unconditionally

The old code mapped `prd` only **inside** the `if symbol_from_db:` branch, so a
symbol-lookup miss left raw Noren values (`C`, `M`, `I`) in the row and the order
book's Modify dialog could not match them. The mapping is now independent of the
lookup:

| Noren `prd` | Exchange | OpenAlgo |
|---|---|---|
| `C` | NSE / BSE | `CNC` |
| `I` | any | `MIS` |
| `M` | NFO / MCX / BFO / CDS | `NRML` |

### 3.2 Price-type mapping

| Noren `prctyp` | OpenAlgo |
|---|---|
| `MKT` | `MARKET` |
| `LMT` | `LIMIT` |
| `SL-MKT`, `SLMKT` | `SL-M` |
| `SL-LMT`, `SLLMT` | `SL` |

### 3.3 The price fallback, in priority order

1. **`avgprc` when `instname` is present and `avgprc > 0`.** Highest priority.
2. **`rprc`** when the mapped price type is `MARKET` or `SL-M` and `prc == 0`.

The order matters: the fallback reads `order["prctyp"]`, which is why the
price-type mapping runs first.

---

## 4. `transform_order_data` — the per-item guard

`mapping/order_data.py#transform_order_data` builds the display rows. It guards
three ways:

- `orders is None` → `[]`
- `not orders` → `[]`
- `not isinstance(order, dict)` → skip that item with a warning

The third guard is the same family as the cancel-all fix: a list that contains a
non-dict element must not crash the whole transform.

---

## 5. `get_open_position` — sum every matching row

`api/order_api.py#get_open_position` is what the smart-order path uses to learn
the current position. Two changes:

### 5.1 It sums, it does not take the first match

```python
net_qty = 0
for position in positions:
    if (position.get("tsym") == tradingsymbol
            and position.get("exch") == exchange
            and position.get("prd") == producttype):
        net_qty += _as_int(position.get("netqty"))
```

A leg can appear **more than once** in the position book (different
`netavgprc`, different `prd` rows). Reading only the first match under-reports
the position, and an under-reported position makes the smart-order delta too
large.

### 5.2 An unreadable book propagates

`_read_book` raises `BrokerReadError`, and `get_open_position` does **not** catch
it. The caller refuses to trade on a book it cannot see. Only a genuinely empty
book reaches the summing loop and yields `"0"`.

`_as_int` is a tolerant int for Noren's string numbers (`"75"`, `"75.0"`, `""`,
`None`) — it returns `0` rather than raising.

---

## 6. `place_smartorder_api` — the flip clamp

### 6.1 What it does

The smart-order path takes a **target position size** and computes the delta
needed to reach it. The engine uses it for "reduce to size" style exits.

### 6.2 The three guards

**Per-symbol lock.** `api/order_api.py#_get_symbol_lock` serialises smart orders
per `symbol:exchange:product`, so two concurrent smart orders on the same contract
cannot both read the same pre-fill book and both compute the same delta.

**Fresh book.** `_invalidate_position_cache` runs before the read, because a book
cached before the previous fill still shows the pre-fill quantity and would make
this order's delta too large.

**The flip clamp.** A target on the far side of zero would **flip** the position —
a long sold past flat becomes a short. A "reduce to size" request never means
that:

```python
flipping = (current_position > 0 and position_size < 0) or (
    current_position < 0 and position_size > 0
)
if flipping and not allow_flip:
    logger.warning(... clamping to flat ...)
    position_size = 0
```

`allow_flip` is opt-in via `"1"` / `"true"` / `"yes"`. This is the last line of
defence against an unintended option SELL, independent of whatever the caller
computed — the same principle as the oversell guard in document 03, applied one
layer earlier.

### 6.3 The delta table

| `position_size` | `current_position` | Action |
|---|---|---|
| `0` | `0` | place `data["action"]` for `data["quantity"]` (a fresh entry) |
| `== current` | any | no order; message says why |
| `0` | `> 0` | SELL `abs(current)` |
| `0` | `< 0` | BUY `abs(current)` |
| `> 0` | `0` | BUY `position_size` |
| `< 0` | `0` | SELL `abs(position_size)` |
| `> current` | any | BUY `position_size - current` |
| `< current` | any | SELL `current - position_size` |

---

## 7. Known gaps (stated, not hidden)

1. **`cancel_all_orders_api` treats an unreadable book as "nothing to cancel".**
   It returns `([], [])` for a `Not_Ok` dict, which the service layer reports as
   `"Canceled 0 orders. Failed to cancel 0 orders."` — a success. That is
   acceptable here (cancel-all is best-effort and the square-off follows), but it
   is the opposite of the square-off rule, and the difference is deliberate:
   a false "nothing to cancel" is harmless, a false "nothing to close" is not.
2. **`map_order_data` still indexes `order["token"]` and `order["exch"]`.** A row
   missing those keys raises. The rows come from the broker's own book, so this
   has not been observed, but it is the same family as the guards above.
3. **`transform_order_data` does not guard `orders` being a bare dict.** It
   guards `None` and empty, and per-item non-dicts, but a top-level dict would
   iterate its keys and hit the per-item guard — which skips them all and returns
   `[]`. Safe, but silently empty rather than reported.
