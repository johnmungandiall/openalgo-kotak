# Kotak adapter — fixable defects

19 defects where the official Kotak Neo Trade API v2 documentation (or the
upstream PR that fixes them) fully specifies the correct behaviour, so the fix
is a code change with a known target. Each entry names the code, the evidence,
why it matters, and the fix.

**All 19 are IMPLEMENTED.** Verified by `test/test_kotak_adapter_fixes.py`
(22 tests), `test/test_kotak_funds_pnl.py` (15 tests) and the 204 existing
tests in `test/test_smartorder_logic.py` — 261 passing. Each entry below keeps
its original evidence and fix description, and records what was actually
changed.

K19 was added after the first 18: it is the funds PnL defect fixed from PR
[`marketcalls/openalgo#1224`](https://github.com/marketcalls/openalgo/pull/1224).

Severity is about blast radius, not effort:

- **Critical** — can lose money or open an unintended position.
- **High** — reports a wrong result the user will act on.
- **Medium** — wrong data or a crash on a path that is reachable but not
  money-critical.
- **Low** — hygiene, or a defect that only shows on an unusual reply.

## Index

| ID | Defect | Req | Severity |
| --- | --- | --- | --- |
| K1 | `close_all_positions` reports success without checking the broker reply | R2/R4 | Critical |
| K2 | `close_all_positions` has no lock, no working-SELL ledger, no oversell guard | R2 | Critical |
| K3 | `close_all_positions` hard-indexes `position["tok"]`, which the doc does not return | R2 | Critical |
| K4 | An unreadable position book reads as a flat book | R3 | Critical |
| K5 | Funds reads three field names the doc does not define | R6 | High |
| K6 | Funds has no cache and no single-flight | R6 | Medium |
| K19 | Funds PnL trusts the limits reply's `rpnl`, which is wrong for closed positions | R6 | High |
| K7 | IOC is unreachable — `rt` and `vd` are hardcoded `DAY` | R1 | High |
| K8 | `map_order_type` defaults to `"MARKET"`, which is not a valid Kotak value | R1 | High |
| K9 | MTF is unmapped, so an MTF order goes out with `pc: null` | R1 | Medium |
| K10 | BO/CO are still mapped although the doc discontinues them | R1 | Medium |
| K11 | Hard-indexed reply fields crash on the documented error shape | R4 | High |
| K12 | Credentials are written to the debug log | R7 | High |
| K13 | `get_open_position` stops at the first matching row | R3 | High |
| K14 | `place_smartorder_api` has no flip clamp | R2 | Critical |
| K15 | `place_smartorder_api` reads a book cached before the previous fill | R3 | High |
| K16 | No rate limiter anywhere in the adapter | R6 | Medium |
| K17 | `parse_margin_response` reads `errMsg`; the doc says `emsg` | R4 | Low |
| K18 | The error branch returns `"NotOk"`; the doc says `"Not_Ok"` | R4 | Low |

---

## K1 — `close_all_positions` reports success without checking the broker reply

**Severity:** Critical · **Req:** R2, R4

**Code.** `broker/kotak/api/order_api.py#close_all_positions`

The function places a close order per open position and then discards the
result:

```python
res, response, orderid = place_order_api(place_order_payload, auth_token)
logger.debug(f"{response}")
```

`res`, `response` and `orderid` are never inspected. After the loop the
function returns unconditionally:

```python
return {"status": "success", "message": "All Open Positions SquaredOff"}, 200
```

**Doc.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`
§5 defines exactly two reply shapes for a placement:

- success — `{"nOrdNo": "...", "stat": "Ok", "stCode": 200}`
- failure — `{"stat": "Not_Ok", "emsg": "Insufficient balance.", "stCode": 1004}`

**Why it matters.** A rejected close is reported to the user as a completed
square-off. The user believes the position is flat, stops watching it, and
carries an open position into the close. This is the single most dangerous
defect in the adapter: it converts a broker rejection into a false statement
about account state.

**Fix.** Honour the reply, exactly as
`broker/flattrade/api/order_api.py#close_all_positions` does:

- treat the close as successful only when the HTTP status is 200 **and**
  `response.get("stat") == "Ok"`;
- collect failures into a list with the symbol and `emsg`;
- return `500` with the failure list when any close failed, instead of `200`
  with a success message;
- return `200` only when every close was accepted.

---

## K2 — `close_all_positions` has no lock, no working-SELL ledger, no oversell guard

**Severity:** Critical · **Req:** R2

**Code.** `broker/kotak/api/order_api.py#close_all_positions`

The function reads the position book, then loops and sells each long. There is
no per-account lock, no record of SELL orders already working at the broker,
and no subtraction of working quantity from the quantity it is about to sell.

**Doc.** Two endpoints supply what the guards need:

- `broker-api-docs/kotak-api-docs/07-portfolio-positions.md` — `flBuyQty`,
  `flSellQty`, `cfBuyQty`, `cfSellQty` give the net position.
- `broker-api-docs/kotak-api-docs/11-order-reports.md` — the order book
  (`/quick/user/orders`) gives `nOrdNo`, `trnsTp` (`B`/`S`), `ordSt`, `qty`;
  the trade book (`/quick/user/trades`) gives `nOrdNo` and `fldQty`.

**Why it matters.** Two sweeps that overlap — a user double-clicking, a retry
after a timeout, two strategies squaring off at once — both read the same
stale net quantity. Each sells the whole long. The second sell takes the
account through zero and opens a **short**, which for an option leg means
selling a contract the account never bought. That is the unintended
option-selling defect the Flattrade guards exist to prevent.

**Fix.** Port the three guards from
`broker/flattrade/api/order_api.py#close_all_positions`:

1. **Per-account lock** — `_get_close_lock(auth_token)`, an account-wide
   `threading.Lock` held for the whole sweep. Account-wide, not per-symbol,
   because one sweep touches every position.
2. **Working-SELL ledger** — build `{(trdSym, exSeg, prod): [remaining]}` from
   the order book, keeping rows where `trnsTp == "S"` and `ordSt` is a working
   state. **Note the constraint:** the order book does not carry a filled
   quantity, so `remaining` must be `qty` minus the `fldQty` summed from the
   trade book for the same `nOrdNo`. The working-state vocabulary is already
   enumerated in `broker/kotak/streaming/kotak_order_adapter.py#_STATUS_MAP`.
3. **Oversell guard** — for a long, subtract the still-working SELL quantity
   from the quantity about to be sent; if the remainder is zero or negative,
   skip the leg and record it as covered rather than selling again.

Also invalidate the position cache before the first read and after the sweep,
so a book cached before a previous close cannot drive the next one.

---

## K3 — `close_all_positions` hard-indexes `position["tok"]`

**Severity:** Critical · **Req:** R2

**Code.** `broker/kotak/api/order_api.py#close_all_positions`

```python
symboltoken = position["tok"]
exchange = map_exchange(position["exSeg"])
position["exSeg"] = exchange
symbol = get_symbol(symboltoken, exchange)
```

**Doc.** `broker-api-docs/kotak-api-docs/07-portfolio-positions.md` §5 lists
the position object's fields: `actId`, `prod`, `exSeg`, `trdSym`, `sym`, `qty`,
`buyAmt`, `sellAmt`, `flBuyQty`, `flSellQty`, `brdLtQty`, `posFlg`, `sqrFlg`,
`lotSz`, `stkPrc`, `hsUpTm`, plus "other available fields" `cfBuyAmt`,
`cfSellAmt`, `cfBuyQty`, `cfSellQty`, `multiplier`, `precision`, `expDt`,
`genNum`, `genDen`, `prcNum`, `prcDen`, `optTp`, `type`.

**`tok` is not among them.** The documented identifier is `trdSym` (the trading
symbol) with `sym` as the short name.

**Why it matters.** A `KeyError` here aborts the whole sweep mid-loop: the
positions already closed stay closed, the rest stay open, and the caller gets
an exception rather than a partial result. Because K1 discards the reply, the
user may still be told the square-off succeeded.

**Fix.** Resolve the symbol from `trdSym` first, falling back to `sym`, using
the same token-then-symbol strategy already implemented in
`broker/kotak/mapping/order_data.py#_openalgo_symbol`. Never index a position
row directly; use `.get()` and treat a missing identifier as a per-position
failure that is reported, not an exception that aborts the sweep.

---

## K4 — An unreadable position book reads as a flat book

**Severity:** Critical · **Req:** R3

**Code.** `broker/kotak/api/order_api.py#get_open_position`

```python
net_qty = "0"
if positions_data.get("data"):
    for position in positions_data["data"]:
        ...
return net_qty
```

A reply with no `data` key — which is exactly what an error reply looks like —
falls through and returns `"0"`.

**Doc.** `broker-api-docs/kotak-api-docs/07-portfolio-positions.md` §5 defines
the error reply as `{"stat": "Not_Ok", "emsg": "Invalid session", "stCode":
1003}` — no `data` key at all. Success is `{"stat": "Ok", "stCode": 200,
"data": [...]}`.

**Why it matters.** "I could not read the book" and "the book is empty" are
different facts, and this code collapses them into the same answer. A smart
order that reads `0` believes the account is flat and opens a fresh position
on top of one that already exists — doubling exposure the user did not ask
for. The same collapse appears in `close_all_positions`
(`positions_response["data"]`) and `cancel_all_orders_api`
(`order_book_response["data"]`), where it raises `KeyError` instead.

**Fix.** Port the read-vs-empty distinction from
`broker/flattrade/api/order_api.py`:

- a `BrokerReadError` exception type;
- `_read_book(response, what)` returning `[]` only for a genuinely empty book
  and raising for anything else;
- `get_open_position` letting the error propagate so the caller refuses to
  trade on a book it cannot see;
- `close_all_positions` and `cancel_all_orders_api` returning a real error
  instead of indexing `["data"]`.

Kotak's empty marker differs from Flattrade's: Kotak signals an empty book
with `stat: "Ok"` and an empty or absent `data` array, not with a `"no data"`
`emsg`. The marker list must be written for Kotak, not copied.

---

## K5 — Funds reads three field names the doc does not define

**Severity:** High · **Req:** R6

**Code.** `broker/kotak/api/funds.py#get_margin_data`

```python
collateral_value = float(margin_data.get("CollateralValue", 0))
pay_in = float(margin_data.get("RmsPayInAmt", 0))
pay_out = float(margin_data.get("RmsPayOutAmt", 0))
collateral = float(margin_data.get("Collateral", 0))

processed_margin_data = {
    "availablecash": f"{collateral_value + pay_in - pay_out + collateral:.2f}",
    ...
}
```

**Doc.** `broker-api-docs/kotak-api-docs/09-portfolio-limits.md` §5 lists the
`/quick/user/limits` response fields: `Category`, `EntityId`, `BoardLotLimit`,
`CollateralValue`, `Net`, `MarginUsed`, `AdhocMargin`, `SpanMarginPrsnt`,
`ExposureMarginPrsnt`, `NotionalCash`, `UnrealizedMtomPrsnt`,
`RealizedMtomPrsnt`, `SpecialMarginPrsnt`, `PremiumPrsnt`, `MarginVarPrsnt`,
`stCode`, `stat`.

`CollateralValue` is documented. **`RmsPayInAmt`, `RmsPayOutAmt` and
`Collateral` are not.** The doc's own description of `Net` is "Net available
margin/cash" — the value the code is trying to compute.

**Why it matters.** `.get(..., 0)` means the three undocumented names silently
evaluate to zero. No error, no warning: `availablecash` becomes
`CollateralValue` alone. A user reading their available funds sees a number
that is not their available funds, and the failure is invisible in the log.

**Fix.** Compute `availablecash` from `Net`, which the doc defines for exactly
this purpose. Keep `CollateralValue` for the `collateral` field and
`MarginUsed` for `utiliseddebits`, both of which the code already reads
correctly. If a fallback to the current formula is kept for older responses,
it must log when it is used — a silent fallback is what hid this defect.

---

## K19 — Funds PnL trusts the limits reply's `rpnl`, which is wrong for closed positions

**Severity:** High · **Req:** R6

**Code.** `broker/kotak/api/funds.py#_fetch_funds` previously read PnL straight
out of the limits reply:

```python
"m2munrealized": f"{float(margin_data.get('UnrealizedMtomPrsnt', 0)):.2f}",
"m2mrealized": f"{float(margin_data.get('RealizedMtomPrsnt', 0)):.2f}",
```

**Doc / upstream.** Kotak's `rpnl` is inaccurate for closed positions — the
reason PR [`marketcalls/openalgo#1224`](https://github.com/marketcalls/openalgo/pull/1224)
exists. The position book carries the authoritative amounts: `buyAmt`,
`sellAmt`, `cfBuyAmt`, `cfSellAmt`, `flBuyQty`, `flSellQty`, `cfBuyQty`,
`cfSellQty` (`07-portfolio-positions.md` §5).

**Why it matters.** A realized PnL that is wrong for closed positions is the
number a user checks to decide whether a strategy worked. It is also the number
the dashboard's PnL display rides on.

**Fix.** Ported from PR #1224, adapted to this tree:

- `safe_float` / `safe_int` — Kotak returns every numeric field as a **string**
  and nulls the ones it has no value for, so a bare `float()` raised.
- `_position_symbol` — resolves a position row to (symbol, exchange) from
  `trdSym`/`sym`, using `tok` only when present. The PR used `tok` alone, which
  is **not** a documented positions field and would silently fail every row.
- `_pnl_from_positions` — computes realized and unrealized from the book:
  - fully closed (`net_qty == 0`): `sellAmt - buyAmt`;
  - partial long: `sellAmt - (avgBuy × sellQty)`;
  - partial short: `(avgSell × buyQty) - buyAmt`;
  - unrealized: `(ltp - avg) × netQty`, buy-average for longs, sell-average for
    shorts;
  - carry-forward amounts and quantities are included throughout.
- LTP is batch-fetched via `BrokerData.get_multiquotes` for open positions only.

**Fallbacks — a wrong number is worse than the broker's own number.** The
limits reply's `RealizedMtomPrsnt` / `UnrealizedMtomPrsnt` are used when the
book is unreadable, empty, the quote fetch throws, or **not one** open position
could be priced. If *some* positions priced, the computed figure stands —
falling back would discard the ones that worked.

**Deliberately NOT taken from the PR.** Its diff predates K5 and still computed
`availablecash` from `RmsPayInAmt` / `RmsPayOutAmt` / `Collateral` — three names
absent from the documented response. Applying it verbatim would have reverted
the verified `Net` fix and reintroduced the silent-zero bug. `availablecash`
stays on `Net`; a regression test pins it.

**Post-K19 collateral fix — PnL was displaying the BALANCE as profit.** The app's
P&L is `collateral + m2mrealized + m2munrealized`
(`docs/openalgo-api-contract.md` §2.1). `collateral` mapped from
`CollateralValue` — which equals `Net` on a cash-only account (verified live:
both 2006.59 with `MarginUsed` 0) — so the dashboard showed P&L +2006.59 with
both M2M legs at 0.00. Flattrade maps `collateral` to `brkcollamt`, the
pledged-SECURITIES figure (`broker/flattrade/api/funds.py#_fetch_funds`), so
Kotak now maps it to its own securities-only field, `Collateral` (0 when
nothing is pledged). `availablecash` still carries the balance, which is what
the BAL pill reads. Tests: `test_collateral_is_pledged_securities_not_the_balance`
asserts the app PnL is 0 on a cash-only account, and
`test_pledged_collateral_still_reported` asserts a pledged amount still flows
through.

**Verified.** `test/test_kotak_funds_pnl.py` — 15 tests covering every branch
above plus the four fallbacks and the K5 guard. Live: `/api/v1/funds` returns
`availablecash=2006.59`, `m2mrealized=0.00`, `m2munrealized=0.00` on an account
with no open positions (empty book → documented fallback).

---

## K6 — Funds has no cache and no single-flight

**Severity:** Medium · **Req:** R6

**Code.** `broker/kotak/api/funds.py#get_margin_data` issues one broker request
per call, with no cache and no de-duplication of concurrent callers.

**Doc.** `broker-api-docs/kotak-api-docs/14-troubleshooting-faqs-error-codes.md`
Q24: "10 requests/second across APIs. Exceeding this returns rate limit
errors."

**Why it matters.** The funds endpoint is polled by the dashboard several times
a second. Every poll is a broker round-trip against a 10/sec account-wide
budget, and concurrent pollers each issue their own. A balance cannot change
that fast, so most of that quota buys nothing — and it is quota the order path
needs.

**Fix.** Port the pattern from `broker/flattrade/api/funds.py#get_margin_data`:
a short-TTL `TTLCache` keyed on a hash of the auth token (never the raw token,
which is a live credential), plus a single-flight so concurrent misses share
one broker fetch. Cache only a usable result — caching an empty dict would pin
"no funds" for the whole TTL.

---

## K7 — IOC is unreachable; `rt` and `vd` are hardcoded `DAY`

**Severity:** High · **Req:** R1

**Code.**

- `broker/kotak/mapping/transform_data.py#transform_data` — `"rt": "DAY"`
- `broker/kotak/mapping/transform_data.py#transform_modify_order_data` —
  `"vd": "DAY"`

Both are literals. No caller can request IOC.

**Doc.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`
§4: `rt` — "Validity / order duration", allowed `DAY`, `IOC`. The modify table
§4: `vd` — "Validity (order duration)", allowed `DAY`, `IOC`.

**Why it matters.** A MARKET order is rewritten to a marketable LIMIT by the
MPP layer. With DAY retention, any unfilled remainder **rests** behind the
touch instead of being cancelled. On an exit that is the difference between a
closed position and a partially closed one that keeps working all day.

**Fix.** Add a `_retention(data)` helper mirroring
`broker/flattrade/mapping/transform_data.py#_retention`: read
`data["validity"]`, upper-case it, accept only `DAY`/`IOC`, default to `DAY`
when absent. Clamp in the mapper rather than trusting the caller, because the
mapper is also reached by internal callers that assemble the dict themselves
(smart-order exit branch, `close_all_positions`, modify).

---

## K8 — `map_order_type` defaults to `"MARKET"`, which is not a valid Kotak value

**Severity:** High · **Req:** R1

**Code.** `broker/kotak/mapping/transform_data.py#map_order_type`

```python
order_type_mapping = {"MARKET": "MKT", "LIMIT": "L", "SL": "SL", "SL-M": "SL-M"}
return order_type_mapping.get(pricetype, "MARKET")
```

The default is the string `"MARKET"`. The mapped values are `"MKT"`, `"L"`,
`"SL"`, `"SL-M"`.

**Doc.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`
§4: `pt` — "Order type", allowed `L` (Limit), `MKT` (Market), `SL` (Stoploss),
`SL-M` (SL-Market).

`"MARKET"` is not in that set.

**Why it matters.** Any pricetype outside the four mapped keys — a typo, a new
value, an empty string — produces `pt: "MARKET"`, which Kotak rejects. The
order fails at the broker with a validation error that points at the order
type rather than at the caller's bad input, which makes it hard to trace.

**Fix.** Default to `"MKT"` (the documented market value), or better, raise on
an unknown pricetype so the caller learns immediately instead of sending an
order that cannot succeed. The same default appears in
`broker/flattrade/mapping/transform_data.py#map_order_type` and has the same
problem there.

---

## K9 — MTF is unmapped, so an MTF order goes out with `pc: null`

**Severity:** Medium · **Req:** R1

**Code.** `broker/kotak/mapping/transform_data.py#map_product_type`

```python
product_type_mapping = {"CNC": "CNC", "NRML": "NRML", "MIS": "MIS"}
return product_type_mapping.get(product)
```

No default. An unmapped product returns `None`, which lands in the payload as
`"pc": null`.

**Doc.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`
§4: `pc` — "Product code", allowed `NRML`, `CNC`, `MIS`, `CO`, `BO`, `MTF`.

`MTF` is a documented product the adapter cannot express.

**Why it matters.** A `null` product code is not a documented value, so the
order is rejected — but only after the payload has been built and sent, and
the error names the product field rather than the missing mapping.

**Fix.** Add `"MTF": "MTF"` to the map. For any other unknown product, fail
before the request with a message naming the product, rather than sending
`null`.

---

## K10 — BO/CO are still mapped although the doc discontinues them

**Severity:** Medium · **Req:** R1

**Code.** `broker/kotak/mapping/transform_data.py#reverse_map_product_type`
maps only `CNC`/`NRML`/`MIS` and returns `None` for everything else, so a
position whose `prod` is `CO` or `BO` produces `"product": None` in the
square-off payload. `map_product_type` has no `CO`/`BO` entry either.

**Doc.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`,
opening note: "Bracket orders (BO) and Cover orders (CO) have been discontinued
on Trade APIs since 1 April 2026." `15-api-reference.md` repeats it and notes
the exit endpoints are retained "for reference" only.

**Why it matters.** Two separate problems. A position opened before the
cutover can still appear in the book with `prod: CO`/`BO`, and squaring it off
would send `product: None`. And a new order requesting CO/BO will be rejected
by the broker, so accepting it at the adapter only delays the error.

**Fix.** Map `CO`/`BO` explicitly so an existing position can still be closed
(the exit endpoints are retained), and reject a *new* CO/BO placement at the
adapter with a clear message rather than forwarding it. Do not simply delete
the mapping — that would strand pre-cutover positions.

---

## K11 — Hard-indexed reply fields crash on the documented error shape

**Severity:** High · **Req:** R4

**Code.** Five places index a reply field that the documented error shape does
not contain:

| Location | Indexed |
| --- | --- |
| `broker/kotak/api/order_api.py#get_api_response` | `json.loads(response.text)` with no guard |
| `broker/kotak/api/order_api.py#place_order_api` | `response_data["stat"]`, `response_data["nOrdNo"]` |
| `broker/kotak/api/order_api.py#cancel_all_orders_api` | `order_book_response["data"]`, `order["ordSt"]` |
| `broker/kotak/mapping/order_data.py#map_order_data` | `order_data["stat"]`, `order_data["data"]` |
| `broker/kotak/mapping/order_data.py#calculate_order_statistics` | `order["trnsTp"]`, `order["ordSt"]` |

**Doc.** Every endpoint in `broker-api-docs/kotak-api-docs/` documents the same
two shapes: success carries `data` (or `nOrdNo`), failure carries `stat:
"Not_Ok"` + `emsg` + `stCode` and **no** `data`. `11-order-reports.md` §5.1
shows the order book's failure reply as a bare object, not an array.

**Why it matters.** A `KeyError` inside a mapper or an API helper surfaces as a
generic HTTP 500 from the service layer, which reads as "the server broke"
rather than "the broker said no". The user retries, and the retry hits the same
wall. It also means a session expiry — the most common failure — is
indistinguishable from a code fault.

**Fix.** Guard every one of them:

- `get_api_response` — wrap `json.loads` and return a normalised error dict on
  a non-JSON body (an edge proxy can answer with HTML);
- `place_order_api` — `response_data.get("stat")`, and read `nOrdNo` only when
  `stat == "Ok"`;
- `cancel_all_orders_api` — check `isinstance(order_book_response, list)`
  before iterating, and use `.get("ordSt")` per row;
- `map_order_data` / `map_trade_data` — `isinstance(..., dict)` and
  `.get("stat")` before touching `["data"]`;
- `calculate_order_statistics` — `.get()` for `trnsTp` and `ordSt`.

The Flattrade equivalents already do this; see
`broker/flattrade/api/order_api.py#cancel_all_orders_api` for the
`isinstance(..., list)` guard and its comment explaining the exact
`AttributeError: 'str' object has no attribute 'get'` it prevents.

---

## K12 — Credentials are written to the debug log

**Severity:** High · **Req:** R7

**Code.**

- `broker/kotak/api/auth_api.py#authenticate_broker` logs the **full** body of
  both login responses:
  - `logger.debug(f"TOTP Login Response: {response.text}")`
  - `logger.debug(f"MPIN Validation Response: {response.text}")`

  Both bodies contain `token` and `sid` in plaintext
  (`broker-api-docs/kotak-api-docs/02-authentication.md` §5 shows the shape).
  The same function then logs a token prefix and the **complete** session sid:
  `AUTH TOKEN CREATED: {trading_token[:10]}...:::{trading_sid}:::{base_url}:::{access_token[:10]}...`

- `broker/kotak/api/data.py#_make_quotes_request` logs
  `access_token: {self.access_token[:10]}...`

- `broker/kotak/api/funds.py#get_margin_data` logs the full limits response.

**Doc.** `broker-api-docs/kotak-api-docs/02-authentication.md` §5 and
`14-troubleshooting-faqs-error-codes.md` Q3 both describe these values as live
session credentials. Q2: resetting the access token "immediately invalidates
all sessions" — which is the remedy once one has leaked.

**Why it matters.** A debug log is a file on disk, shipped in support bundles
and pasted into issue reports. A session token in it is a working trading
credential for the rest of the day. The sid is logged in full, not truncated,
so truncating the token alongside it does not help.

**Fix.** Log the *shape* of the reply, never the body: status code, `stat`,
`stCode`, and `emsg` on failure. For the auth string, log only that it was
built and its component count — no token prefix, no sid. The same rule applies
to `funds.py` and `data.py`. Where a value must be identifiable in a log, log a
short hash of it, as `broker/flattrade/api/funds.py#_account_key` does.

---

## K13 — `get_open_position` stops at the first matching row

**Severity:** High · **Req:** R3

**Code.** `broker/kotak/api/order_api.py#get_open_position`

```python
net_qty = (int(position.get("flBuyQty", 0)) - int(position.get("flSellQty", 0))) + (
    int(position.get("cfBuyQty", 0)) - int(position.get("cfSellQty", 0))
)
break  # Assuming you need the first match
```

The comment states the assumption. It is wrong: a contract can appear in more
than one row — different `prod`, a carry-forward row alongside a day row, a
re-listed series.

**Doc.** `broker-api-docs/kotak-api-docs/07-portfolio-positions.md` §6: "Only
positions with actual trades for the day will be listed." It does not promise
one row per contract, and the `cf` (carry-forward) fields exist precisely
because a contract can hold both a day and a carried position.

**Why it matters.** Under-reporting the position makes the smart-order delta
too large. A target of flat against a true position of 150 that reads as 75
sells 75 and leaves 75 open — the exit silently half-completes.

**Fix.** Sum every matching row instead of breaking, exactly as
`broker/flattrade/api/order_api.py#get_open_position` does. Return the sum as a
string, matching the current contract.

---

## K14 — `place_smartorder_api` has no flip clamp

**Severity:** Critical · **Req:** R2

**Code.** `broker/kotak/api/order_api.py#_place_smartorder_locked_kotak`

The delta arithmetic is:

```python
else:
    if position_size > current_position:
        action = "BUY"; quantity = position_size - current_position
    elif position_size < current_position:
        action = "SELL"; quantity = current_position - position_size
```

With `current_position = 75` and `position_size = -75`, this computes
`SELL 150`.

**Doc.** Not a doc question — this is arithmetic on the documented position
fields. The doc supplies `flBuyQty`/`flSellQty`; the flip is a consequence of
the delta formula.

**Why it matters.** A "reduce to size" request that crosses zero becomes an
order twice the size of the position, taking the account from long 75 to short
75. For an option leg that is selling a contract the account does not own. No
caller asking to reduce a position ever means "and open the opposite one".

**Fix.** Port the clamp from
`broker/flattrade/api/order_api.py#place_smartorder_api`: detect the flip
(`current > 0 and target < 0`, or `current < 0 and target > 0`), clamp
`position_size` to `0` unless the caller explicitly opts in with
`allow_flip`, and log a warning when clamping. This is the last line of defence
independent of whatever the caller computed.

---

## K15 — `place_smartorder_api` reads a book cached before the previous fill

**Severity:** High · **Req:** R3

**Code.** `broker/kotak/api/order_api.py#_place_smartorder_locked_kotak` calls
`get_open_position(...)` directly. `get_open_position` reads through
`_get_cached_positions`, whose TTL is `_POSITION_CACHE_TTL = 1.0` second. The
cache is invalidated *after* a placement, but not *before* the read.

**Doc.** Not a doc question. The doc's position fields are the input; the
staleness is a cache-ordering defect.

**Why it matters.** A book cached before the previous fill still shows the
pre-fill quantity, so this order's delta is computed against a position that
no longer exists. Two smart orders in the same second can each compute a delta
from the same stale snapshot.

**Fix.** Invalidate the position cache before the read, as
`broker/flattrade/api/order_api.py#place_smartorder_api` does with
`_invalidate_position_cache(AUTH_TOKEN)` immediately before
`get_open_position`. Keep the existing post-placement invalidation.

---

## K16 — No rate limiter anywhere in the adapter

**Severity:** Medium · **Req:** R6

**Code.** No limiter exists under `broker/kotak/`. `data.py` has a fixed
`RATE_LIMIT_DELAY = 0.2` sleep between multiquote batches only; the order path,
funds and quotes are uncounted.

**Doc.** `broker-api-docs/kotak-api-docs/14-troubleshooting-faqs-error-codes.md`
Q24: "10 requests/second across APIs. Exceeding this returns rate limit
errors." `04-market-data-instruments.md` lists `429 Too many requests` as a
documented error code.

**Why it matters.** The limit is account-wide and shared with the order path.
An uncounted burst of quote or funds calls can consume the budget an order
needs, and the order fails with a rate-limit error at the worst possible
moment — during an exit.

**Fix.** Port the dual sliding-window limiter from
`broker/flattrade/api/data.py#_reserve_rate_limit_slot` /
`#_apply_rate_limit`, sized to Kotak's documented 10/sec. Note that Kotak
publishes only a per-second figure, not a per-minute one, so the second window
should be sized from the per-second figure rather than invented. Apply it to
every outbound call, including the order path — the Flattrade order path
currently does **not** call its limiter, which is a defect recorded in
`broker/flattrade/docs/10-official-api-mapping.md` §11 and should not be
copied.

---

## K17 — `parse_margin_response` reads `errMsg`; the doc says `emsg`

**Severity:** Low · **Req:** R4

**Code.** `broker/kotak/mapping/margin_data.py#parse_margin_response`

```python
error_message = response_data.get("errMsg", "Failed to calculate margin")
```

**Doc.** `broker-api-docs/kotak-api-docs/10-portfolio-margins.md` §5 error
example: `{"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003}`. The
field is `emsg`. (`errMsg` appears only in the *login* responses, per
`02-authentication.md` — a different endpoint family.)

**Why it matters.** The real reason for a margin failure is discarded and
replaced with the generic "Failed to calculate margin", so a session expiry
looks like a margin-engine fault.

**Fix.** Read `emsg`, keeping `errMsg` as a secondary fallback for the login
family if this helper is ever reused there.

---

## K18 — The error branch returns `"NotOk"`; the doc says `"Not_Ok"`

**Severity:** Low · **Req:** R4

**Code.** `broker/kotak/api/order_api.py#place_order_api`, both `except`
branches:

```python
return None, {"stat": "NotOk", "error": str(e)}, None
```

**Doc.** Every endpoint documents the failure status as `"Not_Ok"` — with the
underscore. `06-trading-place-modify-cancel.md` §5, `07-portfolio-positions.md`
§5, `09-portfolio-limits.md` §5, `11-order-reports.md` §5.1.

**Why it matters.** Any caller that checks `response.get("stat") == "Not_Ok"`
— which is the documented test, and what the fix for K1 will introduce —
misses this branch entirely. The error is then treated as neither success nor
failure.

**Fix.** Use `"Not_Ok"` in both branches, and carry `emsg` rather than `error`
so the shape matches the broker's own. A single normalisation helper for
"turn an exception into a documented error reply" would prevent the next
divergence.

---

## Suggested order of work

Not a schedule — a dependency order. K1 without K4 still reports success on a
book it could not read; K2 without K1 still cannot tell you a close failed.

1. **K4** — the read-vs-empty distinction. Everything else depends on knowing
   whether the book was actually read.
2. **K1, K3** — honour the reply, stop indexing `tok`. Together these make a
   square-off report the truth.
3. **K2, K14** — the oversell guards and the flip clamp. These are the ones
   that prevent an unintended short.
4. **K13, K15** — correct position arithmetic.
5. **K5, K6, K16** — funds correctness and load control.
6. **K7–K11** — order-path correctness.
7. **K12** — credential hygiene. Independent of the rest; do it first if the
   logs are already being shared.
8. **K17, K18** — shape consistency.

All of these changed existing behaviour and were implemented with the owner's
explicit go-ahead ("fix cheyagalavi chesey"). The order above is the order they
were applied in.

## What was changed, file by file

| File | Defects |
| --- | --- |
| `broker/kotak/api/rate_limit.py` (new) | K16 |
| `broker/kotak/api/order_api.py` | K1, K2, K3, K4, K11, K13, K14, K15, K16, K18 |
| `broker/kotak/api/funds.py` | K5, K6, K12, K16, K19 |
| `broker/kotak/mapping/transform_data.py` | K7, K8, K9, K10 |
| `broker/kotak/mapping/order_data.py` | K11 |
| `broker/kotak/mapping/margin_data.py` | K17 |
| `broker/kotak/api/auth_api.py` | K12 |
| `broker/kotak/api/data.py` | K12, K16 |
| `test/test_kotak_adapter_fixes.py` (new) | verification for K1–K18 |
| `test/test_kotak_funds_pnl.py` (new) | verification for K19 |

## Verification

```
.venv/Scripts/python.exe -m pytest test/test_kotak_adapter_fixes.py -q
  -> 22 passed
.venv/Scripts/python.exe -m pytest test/test_smartorder_logic.py -q
  -> 204 passed
```

The 22 tests exercise the real functions with the broker boundary
monkeypatched, so a regression in the guard logic fails there rather than in
production. The money-critical ones are covered explicitly: a rejected close
reported as failure (K1), an unreadable book refusing to trade (K4), a covered
long not sold twice (K2), and a flip clamped to flat (K14).
