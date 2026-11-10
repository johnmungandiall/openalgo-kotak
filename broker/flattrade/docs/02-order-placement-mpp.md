# 02 — Order placement: MARKET emulation, MPP, retention

**Files:** `broker/flattrade/mapping/transform_data.py`,
`broker/flattrade/api/order_api.py`, `utils/mpp_slab.py`

This is the most consequential modification in the plugin. Everything the engine
does — every entry, every exit, every square-off — passes through it.

---

## 1. The constraint that forces everything

Flattrade runs the **Noren OMS**, which **rejects a raw `MKT` and a raw `SL-MKT`
for API orders**. The engine, however, places `MARKET` for entries and for
square-off, because that is the only price type that guarantees a fill.

So the plugin must rewrite the price type at the mapper boundary. There is no
configuration flag for this and no way to opt out: an order that reaches the OMS
as `MKT` is dead on arrival.

The failure this replaced was not a rejection — it was a **silent degradation**.
The old code fetched a quote to price the limit; when that quote failed it logged
a warning and fell through to the raw `MKT`. The OMS then rejected the order
(`0/390 Rejected`) while the engine still believed the position had moved or
closed. Three SELLs were rejected this way at 09:15:24 and the engine entered the
opposite leg.

---

## 2. MARKET → LMT

### 2.1 The pricing rule

`transform_data.py#_marketable_limit_price` builds the limit in three steps:

1. **MPP off the LTP.** `utils/mpp_slab.py#calculate_protected_price` moves the
   price a slab percentage away from the LTP — up for a BUY, down for a SELL.
2. **Cross to the touch.** MPP alone can still rest *behind* the best bid when
   the premium is moving fast, which is exactly what a square-off meets. So:
   - BUY: if `ask > protected`, use the ask.
   - SELL: if `bid < protected`, use the bid.
3. **Round in the direction that keeps it crossing.**
   - SELL → `floor` (never rounds *up* into an unfillable price)
   - BUY → `ceil` (never rounds *down* and fails to reach the offer)

Step 3 exists because of a real float trap: `102.0 / 0.05` is
`2039.9999999999998` in binary float, and a naive floor drops a whole tick.
`utils/mpp_slab.py#round_to_tick_size` rounds the ratio to 6 decimals before
flooring for exactly this reason.

### 2.2 The slab

`utils/mpp_slab.py` owns two tables, shared by every broker:

| Instrument | Price | Protection |
|---|---|---|
| EQ / FUT | `< 100` | 2.0 % |
| EQ / FUT | `100 – 500` | 1.0 % |
| EQ / FUT | `> 500` | 0.5 % |
| CE / PE | `< 10` | 5.0 % |
| CE / PE | `10 – 100` | 3.0 % |
| CE / PE | `100 – 500` | 2.0 % |
| CE / PE | `> 500` | 1.0 % |

Instrument type is derived from the symbol suffix by
`utils/mpp_slab.py#get_instrument_type_from_symbol` (`CE`, `PE`, `FUT`, else `EQ`).

### 2.3 The quote dependency, and why it now fails loudly

The quote is the single thing standing between a square-off and a dead order, so
it is treated as a best-effort dependency with a bounded cost:

`transform_data.py#_reference_quote`:

- **2 attempts**, 0.15 s apart (`_MPP_QUOTE_ATTEMPTS`, `_MPP_RETRY_GAP_SECONDS`).
- A successful quote is cached per `(symbol, exchange)` for **60 s**
  (`_MPP_CACHE_TTL_SECONDS`).
- If both attempts fail, a cached quote is used — with a `WARNING` that names the
  LTP, bid and ask it is reusing.
- If there is no cache either, it **raises `MPPPriceUnavailable`**.

`MPPPriceUnavailable` is deliberately an exception and not a sentinel return.
A sentinel can be ignored by a caller; an exception cannot. The comment in the
code says it directly: *"RAISE, never return a sentinel: the caller must not be
able to send a raw `MKT`."*

### 2.4 The final guards

Even after the MPP branch, `transform_data` re-checks the outcome before building
the payload:

```python
if original_type == "SL-M" and order_type != "SL-LMT":
    raise MPPPriceUnavailable(...)
if original_type == "MARKET" and order_type != "LMT":
    raise MPPPriceUnavailable(...)
```

These catch every path that could otherwise leak a raw `MKT` through: no auth
token, an LTP of zero, an unexpected exception inside the MPP block. The order is
refused, and `api/order_api.py#place_order_api` never runs.

### 2.5 Tick size

Tick size comes from the quote (`ti` field, surfaced as `tick_size` by
`api/data.py#BrokerData.get_quotes`). When the quote omits it,
`transform_data.py#_tick_size_from_master` falls back to the instrument master
(`symtoken.tick_size`). Tick rounding is not cosmetic — an off-tick price is
rejected by the exchange.

---

## 3. SL-M → SL-LMT — priced off the TRIGGER, never the LTP

This is the subtle one, and getting it wrong is a guaranteed rejection.

A stop-limit must keep its **limit on the protective side of its own trigger**:

- BUY: `prc > trgprc`
- SELL: `prc < trgprc`

Pricing an SL-M off the LTP breaks that relation whenever price and trigger
disagree. The concrete case from the code comment: a BUY triggered at 110 with
the LTP at 100 went out at 102 — **below its own trigger** — and the OMS
rejected it.

So `transform_data.py#transform_data` prices an SL-M off
`data["trigger_price"]` via `utils/mpp_slab.py#slm_protected_price`, which:

- rounds BUY `ceil` and SELL `floor`, keeping the limit beyond the trigger;
- then calls `utils/mpp_slab.py#snap_beyond_trigger`, which steps a full tick out
  if the buffer was thinner than one tick (a sub-tick buffer rounds straight back
  onto the trigger), and re-checks the strict side;
- raises `ValueError` if the required side cannot be reached at all.

An SL-M needs **no quote** — the instrument master owns the tick size. That keeps
a stop-loss independent of a live feed, which matters precisely when the feed is
the thing that is broken.

---

## 4. Modify orders

`transform_data.py#transform_modify_order_data` applies the **same** conversion,
because Noren's `ModifyOrder` also accepts only `LMT` / `SL-LMT`. Two extra
guards exist here that the place path does not need:

- **A modify without an action is refused.** The direction decides which side of
  the trigger the limit lands on, so it cannot be guessed:
  `raise MPPPriceUnavailable(... without an action ...)`.
- **A MARKET modify without an auth token is refused.** There is no way to price
  it, and guessing is worse than failing.

`api/order_api.py#modify_order` catches `(MPPPriceUnavailable, ValueError)` and
returns **400** with the reason, so the caller sees a clear refusal rather than a
broker rejection.

Note also: `data["symbol"]` is kept in **OpenAlgo** format through the modify
path, because the MPP quote / slab / tick-size lookups are all keyed on the
OpenAlgo symbol. The broker symbol is derived locally via `get_br_symbol`.

---

## 5. Retention — DAY vs IOC

`transform_data.py#_retention` reads `data["validity"]` and **clamps** it:

```python
value = str((data.get("validity") or "")).strip().upper()
return value if value in ("DAY", "IOC") else "DAY"
```

Why this matters for the engine: a MARKET order is rewritten to a marketable
LIMIT. With `DAY` retention an unfilled remainder **rests at the touch for the
session** — the order looks placed while the position is not there. `IOC`
cancels whatever did not fill immediately.

The clamp exists because a mapper is reachable from internal callers that
assemble the dict themselves (the smart-order exit branch, `close_all_positions`,
modify) and never pass through the schema. An unknown string would reach the OMS
and be rejected; an absent value keeps the legacy `DAY` every existing install
relies on.

The engine sends `IOC` by default, so in practice every app-placed entry is IOC.

---

## 6. Caller-supplied slippage — `mpp_percentage`

`utils/mpp_slab.py#custom_percentage_from` reads an optional `mpp_percentage` out
of the order dict. When present and valid it **replaces the slab lookup
entirely**, so the caller decides the buffer.

Validation lives in `utils/mpp_slab.py#normalise_custom_percentage`:

- `None`, `""` or blank → `None` (the slab decides).
- Non-finite (`nan`, `inf`) → `ValueError`.
- `<= 0` → `ValueError`.
- `>= 100` → `ValueError`. At 100 % a SELL limit prices to zero and the exchange
  refuses it, which is why the ceiling exists.

`calculate_protected_price` additionally refuses a percentage that drives the
limit to zero or below, and refuses a rounding result that lands on zero.

**The key is the switch.** There is no "send 0 to mean default" — the engine
omits `mpp_percentage` entirely when the user picks `Auto`, and the slab decides.

### Two limits the setting does not buy

1. **It is a floor on aggressiveness, not a price cap.** Because the limit is
   then crossed to the touch, a book wider than the buffer makes the order follow
   the touch out. "0.5 %" does not mean "never pay more than 0.5 % worse".
2. **Square-off ignores it.** `close_all_positions` builds its payload without
   the key, so every `closeposition` sweep prices off the slab default. Only the
   `placeorder` path honours the user's figure.

---

## 7. Behaviour matrix — `transform_data`

| Input | Before | After |
|---|---|---|
| `MARKET`, quote OK, LTP > 0 | `MKT` (rejected by OMS) | `LMT` at the marketable price |
| `MARKET`, quote fails twice, cache warm | `MKT` (rejected) | `LMT` at the cached reference price, `WARNING` logged |
| `MARKET`, quote fails twice, no cache | `MKT` (rejected) | **`MPPPriceUnavailable` — order NOT sent** |
| `MARKET`, no auth token | `MKT` (rejected) | **`MPPPriceUnavailable` — order NOT sent** |
| `MARKET`, LTP is 0 | `MKT` (rejected) | **`MPPPriceUnavailable` — order NOT sent** |
| `SL-M`, trigger > 0, tick known | `SL-MKT` (rejected) | `SL-LMT` beyond the trigger |
| `SL-M`, trigger is 0 | `SL-MKT` (rejected) | **`MPPPriceUnavailable` — order NOT sent** |
| `LIMIT` | `LMT`, unchanged | `LMT`, unchanged |
| `SL` | `SL-LMT`, unchanged | `SL-LMT`, unchanged |
| `validity: "ioc"` | `ret: "DAY"` (hardcoded) | `ret: "IOC"` |
| `validity: "GTC"` | `ret: "DAY"` | `ret: "DAY"` (clamped) |
| `mpp_percentage: 1.5` | ignored | 1.5 % replaces the slab |
| `mpp_percentage: 100` | ignored | `ValueError` |

---

## 8. Known gaps (stated, not hidden)

1. **`mpp_percentage` does not reach square-off.** `close_all_positions` builds
   its own payload without the key. A user who sets 0.5 % still gets the slab
   default on every square-off.
2. **The 60-second MPP cache is per-process and unbounded.** It is keyed by
   `(symbol, exchange)` and never evicted except by TTL check on read. On a
   long-running process with a large universe this grows without a ceiling.
3. **A cached quote can be 60 seconds stale.** That is deliberate — a stale
   reference price beats a refused square-off — but it means a fast-moving
   premium can be priced off a price that no longer exists. The `WARNING` names
   the values so it is diagnosable after the fact.
