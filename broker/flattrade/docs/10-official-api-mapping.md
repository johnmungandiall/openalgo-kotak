# 10 — Official Flattrade API → adapter mapping

**Purpose:** show, endpoint by endpoint and field by field, how the official
Flattrade PiConnect API maps onto `broker/flattrade/`. This is the reference for
"what does the adapter actually send, and what does it do with what comes back".

**Sources.** The official API reference is
`marketcalls/broker-api-docs` → `flattrade-api-docs/` (the mirror the adapter's
own docstrings cite as `broker-api-docs/flattrade-api-docs/…`), read on
2026-11-10. The adapter side is read from the live tree. Where the two disagree,
both are stated — the disagreement is the finding.

---

## 1. Endpoint inventory

Every official endpoint, and whether the adapter uses it.

| Official endpoint | Used? | Where |
|---|---|---|
| `POST /PlaceOrder` | ✅ | `api/order_api.py#place_order_api` |
| `POST /ModifyOrder` | ✅ | `api/order_api.py#modify_order` |
| `POST /CancelOrder` | ✅ | `api/order_api.py#cancel_order` |
| `POST /OrderBook` | ✅ | `api/order_api.py#get_order_book` |
| `POST /TradeBook` | ✅ | `api/order_api.py#get_trade_book` |
| `POST /PositionBook` | ✅ | `api/order_api.py#get_positions` |
| `POST /Holdings` | ✅ | `api/order_api.py#get_holdings` |
| `POST /Limits` | ✅ | `api/funds.py#_fetch_funds` |
| `POST /GetBasketMargin` | ✅ | `api/margin_api.py#calculate_margin_api` |
| `POST /GetQuotes` | ✅ | `api/data.py#BrokerData.get_quotes` |
| `POST /TPSeries` | ✅ | `api/data.py#BrokerData.get_history` (intraday) |
| `POST /EODChartData` | ✅ | `api/data.py#BrokerData.get_history` (daily) |
| `wss://…/PiConnectWSAPI/` | ✅ | `streaming/flattrade_websocket.py` |
| `POST /ExitSNOOrder` | ❌ | — |
| `POST /GetOrderMargin` | ❌ | — |
| `POST /MultiLegOrderBook` | ❌ | — |
| `POST /SingleOrdHist` | ❌ | — |
| `POST /ProductConversion` | ❌ | — |
| `POST /GetIndexList` | ❌ | — |
| `POST /TopListName` | ❌ | — |
| `POST /TopList` | ❌ | — |
| `POST /GetOptionChain` | ❌ | — |
| `POST /GetOptionGreek` | ❌ | — |
| `POST /ExchMsg` | ❌ | — |
| `POST /GetBrokerMsg` | ❌ | — |
| `POST /SpanCalc` | ❌ | — |
| `POST /GTT` family | ❌ | — |
| `POST /Postback` (webhook) | ❌ | — |

**12 of ~28 endpoints are used.** The unused ones are covered in §10.

---

## 2. Transport shape — identical everywhere

Every REST call is a `POST` to
`https://piconnect.flattrade.in/PiConnectAPI/<Endpoint>` with a
**form-encoded body of exactly two fields**:

| Field | Content |
|---|---|
| `jData` | the JSON object for that endpoint |
| `jKey` | the access token from login |

The adapter builds this in two places, and they differ slightly:

| Builder | Body | Headers |
|---|---|---|
| `api/order_api.py#get_api_response` | `"jData=" + data + "&jKey=" + AUTH_TOKEN` | `application/json` (except Holdings → `x-www-form-urlencoded`) |
| `api/data.py#get_api_response` | `"jData=" + json.dumps(data) + "&jKey=" + AUTH_TOKEN` | `x-www-form-urlencoded` |

Both work — the broker parses the body, not the header — but the inconsistency is
real and worth knowing before adding a third caller.

`api/order_api.py#get_api_response` also special-cases **Holdings**, which is the
one endpoint whose `jData` must carry `prd`:

```python
if endpoint == "/PiConnectAPI/Holdings":
    data = f'{{"uid": "{api_key}", "actid": "{api_key}", "prd": "C"}}'
```

That matches the official field table, where `prd` is **mandatory** for
`/Holdings` and absent from every other book endpoint.

---

## 3. Authentication

### 3.1 Official flow

| Step | Official |
|---|---|
| 1 | Open `https://auth.flattrade.in/?app_key=APIKEY` |
| 2 | User enters Client ID (UCC), password, PAN/DOB |
| 3 | Redirect to the registered Redirect URL with `?request_code=…` |
| 4 | `POST https://authapi.flattrade.in/trade/apitoken` |
| 5 | Response carries `token` |

Step 4 body:

| Field | Official meaning |
|---|---|
| `api_key` | the public API key |
| `request_code` | the one-time code (valid a few minutes) |
| `api_secret` | **SHA-256 of `(api_key + request_code + api_secret)`** |

Response: `{ "token", "client", "status": "Ok", "emsg" }`.

### 3.2 Adapter

`api/auth_api.py#authenticate_broker`:

```python
BROKER_API_KEY = full_api_key.split(":::")[1]      # the API key, not the uid
BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")
hash_input = f"{BROKER_API_KEY}{code}{BROKER_API_SECRET}"
security_hash = hashlib.sha256(hash_input.encode()).hexdigest()
data = {"api_key": BROKER_API_KEY, "request_code": code, "api_secret": security_hash}
```

| Official | Adapter | Match |
|---|---|---|
| `api_key` | `BROKER_API_KEY` = `BROKER_API_KEY.split(":::")[1]` | ✅ |
| `request_code` | `code` (the callback param) | ✅ |
| `api_secret` = SHA-256 of the concatenation | `sha256(f"{key}{code}{secret}")` | ✅ |
| response `token` | returned as the auth token | ✅ |
| response `status`/`stat` | adapter checks `stat == "Ok"` **and** `"token" in response_data` | ⚠️ see §11.1 |

`api/auth_api.py#authenticate_broker_oauth` is a second, near-identical
implementation of the same exchange. Both exist; only one is wired per login path.

### 3.3 The `:::` composite

Official: `uid`/`actid` is the **user id** (`FZ00000`); `api_key` is a separate
credential. The adapter packs both into one env var, `userid:::api_key`:

| Consumer | Takes | Why |
|---|---|---|
| `api/auth_api.py#authenticate_broker` | `[1]` — the API key | the token exchange needs the key |
| `api/order_api.py#get_api_response` | `[0]` — the uid | `jData.uid` / `jData.actid` |
| `api/data.py#get_api_response` | `[0]` — the uid | same |
| `api/funds.py#_fetch_funds` | `[0]` — the uid | same |
| `streaming/flattrade_adapter.py#initialize` | `[0]` — the uid | WS `uid` / `actid` |
| `streaming/flattrade_order_adapter.py#create_flattrade_order_adapter` | `[0]` — the uid | WS `uid` / `actid` |

The split direction is the single most error-prone thing in this plugin: `[1]` for
auth, `[0]` everywhere else. Getting it backwards produces a login that succeeds
and every subsequent call that fails.

### 3.4 Token lifetime — a documented discrepancy

| Source | Says |
|---|---|
| Official docs | "valid for 24 hours… **cleared between 5 AM and 6 AM** — regenerate after 6 AM" |
| `streaming/flattrade_adapter.py#_setup_connection_management` comment | "the daily **~3 AM IST** token rollover" |
| `broker-integration/references/auth-and-login.md` | "Indian broker tokens expire daily at roughly **3:00 AM IST**" |

The adapter's behaviour is unaffected — it re-reads the token on every reconnect
regardless of the hour — but the comment names the wrong window. See §11.2.

---

## 4. PlaceOrder — field by field

### 4.1 Official request fields

| Field | Req | Official values |
|---|---|---|
| `uid` | * | logged-in user id |
| `actid` | * | account id |
| `exch` | * | NSE / NFO / BSE / MCX |
| `tsym` | * | contract; **URL-encode special chars** (e.g. `M&M`) |
| `qty` | * | numeric |
| `prc` | * | numeric, **cannot be zero** |
| `trgprc` | | SL / SL-M only |
| `dscqty` | * | disclosed qty (max 10 % NSE, 50 % MCX) |
| `prd` | * | `C` CNC / `M` NRML / `H` CO / `B` BO / `I` MIS / `F` MTF |
| `trantype` | * | `B` / `S` |
| `prctyp` | * | **`LMT` / `SL-LMT`** |
| `ret` | * | **`DAY` / `EOS` / `IOC`** |
| `remarks` | | user tag |
| `ordersource` | | `API` |
| `bpprc` / `blprc` / `trailprc` | | bracket / cover only |
| `amo` | * | `Yes` for AMO; **omit entirely otherwise** |
| `tsym2`/`trantype2`/`qty2`/`prc2` | | price type `2L` |
| `tsym3`/… | | price type `3L` |

### 4.2 Adapter payload

`mapping/transform_data.py#transform_data` builds:

```python
transformed = {
    "uid": data["apikey"], "actid": data["apikey"],
    "exch": data["exchange"], "tsym": symbol,
    "qty": str(data["quantity"]), "prc": price,
    "trgprc": str(data.get("trigger_price", "0")),
    "dscqty": str(data.get("disclosed_quantity", "0")),
    "prd": map_product_type(data["product"]),
    "trantype": "B" if action == "BUY" else "S",
    "prctyp": order_type,
    "mkt_protection": "0",
    "ret": _retention(data),
    "ordersource": "API",
}
```

### 4.3 The mapping table

| Official field | Adapter source | Notes |
|---|---|---|
| `uid` | `data["apikey"]` | set by `place_order_api` from `BROKER_API_KEY[0]` |
| `actid` | same as `uid` | Flattrade uses one id for both |
| `exch` | `data["exchange"]` | OpenAlgo exchange, passed through unchanged |
| `tsym` | `get_br_symbol(symbol, exchange)` | `&` → `%26` applied first |
| `qty` | `str(data["quantity"])` | |
| `prc` | `price` | MPP-rewritten for MARKET/SL-M; otherwise the caller's price |
| `trgprc` | `str(data.get("trigger_price", "0"))` | **always sent**, even for LIMIT |
| `dscqty` | `str(data.get("disclosed_quantity", "0"))` | defaults `"0"` |
| `prd` | `map_product_type(product)` | see §4.4 |
| `trantype` | `"B"` / `"S"` | |
| `prctyp` | `order_type` | see §4.5 |
| `ret` | `_retention(data)` | see §4.6 |
| `ordersource` | `"API"` | literal |
| `mkt_protection` | `"0"` | **not an official field** — see §11.3 |
| `amo` | **not sent** | correct: docs say omit entirely when not an AMO |
| `remarks` | **not sent** | optional |
| `bpprc`/`blprc`/`trailprc` | **not sent** | bracket/cover not exposed |
| `tsym2`/`tsym3` family | **not sent** | multi-leg not exposed |

### 4.4 `prd` — product

| OpenAlgo | Adapter sends | Official meaning |
|---|---|---|
| `CNC` | `C` | Cash & Carry |
| `NRML` | `M` | Normal (F&O) |
| `MIS` | `I` | Intraday |
| anything else | `I` | **silent default to MIS** |

Official also defines `H` (Cover), `B` (Bracket), `F` (MTF). The adapter never
sends them, and `reverse_map_product_type` returns **`None`** for them — see
§11.4.

### 4.5 `prctyp` — price type, and why MPP exists

`mapping/transform_data.py#map_order_type` is a straight enum map:

| OpenAlgo | `map_order_type` returns | Official accepts? |
|---|---|---|
| `MARKET` | `MKT` | ❌ **not in the official list** |
| `LIMIT` | `LMT` | ✅ |
| `SL` | `SL-LMT` | ✅ |
| `SL-M` | `SL-MKT` | ❌ **not in the official list** |

**This table is the entire justification for the MPP layer.** The official
`prctyp` enum is `LMT` / `SL-LMT` only (plus `2L`/`3L` for multi-leg). A raw
`MKT` or `SL-MKT` is not a documented value, which is exactly why
`transform_data` rewrites MARKET → `LMT` and SL-M → `SL-LMT` before the payload is
built, and why it **raises `MPPPriceUnavailable`** rather than letting either
through. See `02-order-placement-mpp.md`.

### 4.6 `ret` — retention

| Official | Adapter `_retention` |
|---|---|
| `DAY` | `DAY` |
| `IOC` | `IOC` |
| **`EOS`** | **`DAY`** — clamped away |

The adapter's clamp is `value if value in ("DAY", "IOC") else "DAY"`. `EOS`
(End Of Session) is a documented value that the adapter silently converts to
`DAY`. See §11.5.

### 4.7 `prc` cannot be zero

Official: *"Order price (numeric, cannot be zero)"*.

The adapter sends `price = str(data.get("price", "0"))` for a LIMIT order. A
caller that sends `LIMIT` with price `0` produces `prc: "0"` and the broker
rejects it. The MPP path never produces zero (it raises first), so this only
affects a direct LIMIT caller.

### 4.8 Response

Official: `{ "request_time", "stat": "Ok", "norenordno": "…" }`, or
`{ "stat": "Not_Ok", "emsg": "…" }`.

`api/order_api.py#place_order_api`:

```python
response_data = res.json()
res.status = res.status_code          # services check res.status == 200
if response_data["stat"] == "Ok":
    orderid = response_data["norenordno"]
else:
    orderid = None
return res, response_data, orderid
```

| Official | Adapter |
|---|---|
| `stat` | read; `"Ok"` → orderid, else `None` |
| `norenordno` | returned as `orderid` |
| `emsg` | passed through in `response_data` |
| HTTP status | copied onto `res.status` — **required**, the service layer reads it |

Note `response_data["stat"]` is a **hard index**, not `.get()`. A reply without
`stat` raises `KeyError` here. See §11.6.

---

## 5. ModifyOrder and CancelOrder

### 5.1 ModifyOrder

Official fields: `exch`*, `norenordno`*, `prctyp`, `prc`*, `qty`*, `tsym`*,
`ret`*, `trgprc`, `uid`*, `bpprc`/`blprc`/`trailprc`.

Two official notes that matter:

- **`qty` is the NEW TOTAL quantity** (open + already filled), not the pending
  quantity.
- **`tsym` cannot be changed** — it must match the original order.

`mapping/transform_data.py#transform_modify_order_data` builds:

| Official | Adapter |
|---|---|
| `uid` | `data["apikey"]` |
| `exch` | `data["exchange"]` |
| `norenordno` | `data["orderid"]` |
| `prctyp` | MPP-converted (`LMT` / `SL-LMT`) |
| `prc` | MPP-converted price |
| `qty` | `str(data["quantity"])` |
| `tsym` | `get_br_symbol(oa_symbol, exchange)` |
| `ret` | `_retention(data)` |
| `dscqty` | `str(data.get("disclosed_quantity") or 0)` — **not an official ModifyOrder field** |
| `trgprc` | **only** for `SL` / `SL-M` |

The conditional `trgprc` is deliberate and documented in the code: *"Sending
trgprc=0 for LIMIT orders causes 'Trigger price invalid - 0.00' error."* The
official table lists `trgprc` as optional, which is consistent.

`api/order_api.py#modify_order` catches `(MPPPriceUnavailable, ValueError)` and
returns **400** with the reason, so an unpriceable modify is a clear refusal
rather than a broker rejection.

### 5.2 CancelOrder

Official: `norenordno`*, `uid`*. Response `{ "stat": "Ok", "result": "<norenordno>" }`.

`api/order_api.py#cancel_order` sends exactly `{"uid": api_key, "norenordno": orderid}`
and returns `({"status": "success", "orderid": orderid}, 200)` on `stat == "Ok"`.

One mismatch: on failure the adapter reads `data.get("message", …)`, but the
official failure field is **`emsg`**. So a failed cancel reports the generic
`"Failed to cancel order"` instead of the broker's reason. See §11.7.

---

## 6. Book endpoints

### 6.1 PositionBook

Official request: `uid`*, `actid`*. Response: an **array** of position objects.

Fields the adapter reads:

| Official field | Adapter use |
|---|---|
| `tsym`, `exch`, `prd` | `_position_key()` — the join key against the order book |
| `token` | `get_symbol(token, exch)` → OpenAlgo symbol |
| `netqty` | `_as_int(...)`; `0` skipped, `>0` → SELL, `<0` → BUY |
| `netavgprc`, `lp`, `rpnl`, `urmtom`, `prcftr` | `mapping/order_data.py#transform_positions_data` and `api/funds.py#calculate_pnl` |

**The official failure shape is verbatim what the adapter's guard keys on:**

```
{ "stat": "Not_Ok", "request_time": "...", "emsg": "Error Occurred : 5 \"no data\"" }
```

That string is why `api/order_api.py#_EMPTY_BOOK_MARKERS = ("no data",)` exists,
and why every *other* `Not_Ok` is treated as a genuine read failure. The official
docs confirm the shape; they do not say it is the only empty-book shape, which is
why the guard is narrow rather than broad. See `03-squareoff-safety.md`.

### 6.2 OrderBook

Official request: `uid`*, optional `prd`. Response: an array.

Fields the adapter reads:

| Official field | Adapter use |
|---|---|
| `trantype` | `_working_sell_orders` — only `S` |
| `status` | `normalize_order_status(...) == "open"` |
| `qty`, `fillshares` | `remaining = qty - fillshares` |
| `norenordno` | the id to cancel |
| `tsym`, `exch`, `prd` | the join key |
| `prctyp`, `prc`, `avgprc`, `rprc`, `instname` | `mapping/order_data.py#map_order_data` |
| `rejreason` | surfaced on a rejected order |

Official failure: `{ "stat": "Not_Ok", "request_time": "...", "emsg": "..." }` —
a **bare dict**, which is exactly the shape that crashed `cancel_all_orders_api`
before the `isinstance(..., list)` guard. See `04-cancel-all-and-books.md`.

### 6.3 TradeBook

Official request: `uid`*, `actid`*. Response fields include `fltm` (fill time),
`flid`, `flqty`, `flprc`, `ordersource`, `cstFrm`.

`mapping/order_data.py#transform_tradebook_data` reads `norentm` for the
timestamp — **`norentm` is not in the official TradeBook field list**; the
documented fill timestamp is `fltm`. See §11.8.

### 6.4 Holdings

Official request: `uid`*, `actid`*, **`prd`***. Response fields: `exch_tsym[]`,
`holdqty`, `dpqty`, `npoadqty`, `colqty`, `benqty`, `unplgdqty`, `brkcolqty`,
`btstqty`, `btstcolqty`, `usedqty`, `upldprc`.

The adapter's valuation formula in
`mapping/order_data.py#calculate_portfolio_statistics`:

```
(btstqty + holdqty + brkcolqty + unplgdqty + benqty + max(npoadqty, dpqty) - usedqty) * upldprc
```

matches the official **Valuation** formula exactly. The official docs also give a
**Salable** formula (`btstqty + holdqty + unplgdqty + benqty + dpqty - usedqty`)
which the adapter does **not** implement — there is no salable-quantity field in
the OpenAlgo holdings contract.

---

## 7. Funds, limits, margin

### 7.1 Limits

Official response fields the adapter reads:

| Official field | Adapter use |
|---|---|
| `cash` | `total_available_margin = cash + payin - marginused` |
| `payin` | same |
| `marginused` | same, and `utiliseddebits` |
| `brkcollamt` | `collateral` |

The official response also carries `urmtom` (unrealized MTOM) at the Limits
level. The adapter **ignores it** and instead sums `urmtom` across PositionBook
rows in `api/funds.py#_fetch_funds`. Both are valid sources; the adapter's choice
means the funds call needs the position book, which is why the two endpoints are
issued together.

### 7.2 GetBasketMargin

Official response: `stat`, `remarks`, `marginused` (**"Total margin"**),
`marginusedtrade` (**"Margin after trade"**), `emsg`.

`mapping/margin_data.py#parse_margin_response`:

```python
margin_used = float(response_data.get("marginused", 0) or 0)
return {"status": "success", "data": {
    "total_margin_required": margin_used, "span_margin": 0, "exposure_margin": 0}}
```

The code comment says this parallels Zerodha's `initial.total` vs `final.total`
and maps total to `marginused`. The official field descriptions confirm that
reading exactly. `span`/`exposure` are hardcoded `0` because Flattrade gives no
breakdown on this endpoint — the breakdown exists only on `/Limits` (the
`span_d_i`, `expo_d_m`, … family).

### 7.3 The basket payload shape

`transform_margin_positions` returns the **first** order flattened to the top
level plus the rest in `basketlists`:

```python
return {"uid":…, "actid":…, "exch": first["exch"], "tsym": first["tsym"], …,
        "basketlists": rest}
```

That matches the official `GetBasketMargin` contract, where the base fields are a
single order and `basketlists` is the optional array of additional orders.

---

## 8. Market data REST

### 8.1 GetQuotes

Official request: `uid`, `exch`, `token`. Response fields the adapter reads:

| Official | Adapter key |
|---|---|
| `bp1` / `sp1` | `bid` / `ask` |
| `o` / `h` / `l` / `lp` / `c` | `open` / `high` / `low` / `ltp` / `prev_close` |
| `v` | `volume` |
| `oi` | `oi` |
| `ti` | `tick_size` |

`ti` (tick size) is the field that makes MPP tick-valid. When it is absent the
adapter falls back to `symtoken.tick_size` via
`mapping/transform_data.py#_tick_size_from_master`.

`get_depth` uses the **same endpoint** and reads `bp1..bp5`, `bq1..bq5`,
`bo1..bo5`, `sp1..sp5`, `sq1..sq5`, `so1..so5` — all documented depth fields.

### 8.2 TPSeries (intraday history)

Official `intrv` values: **1 / 3 / 5 / 10 / 15 / 30 / 60 / 120**.

`api/data.py#BrokerData.timeframe_map`:

| OpenAlgo | `intrv` | Official? |
|---|---|---|
| `1m` | `1` | ✅ |
| `3m` | `3` | ✅ |
| `5m` | `5` | ✅ |
| `10m` | `10` | ✅ |
| `15m` | `15` | ✅ |
| `30m` | `30` | ✅ |
| `1h` | `60` | ✅ |
| `2h` | `120` | ✅ |
| `D` | `D` | routed to EODChartData instead |

Exact match, no gaps.

Official response fields: `time`, `into`, `inth`, `intl`, `intc`, `intvwap`,
`intv`, `intoi`, `v`, `oi`. The adapter reads `into`/`inth`/`intl`/`intc`/`intv`
and `oi`, and **skips candles where all four OHLC values are zero** — a real
condition on illiquid contracts.

**Timestamp format discrepancy:** the official field table says
`DD/MM/CCYY hh:mm:ss` (slashes) but the official sample response shows
`"time": "02-06-2020 15:46:23"` (hyphens). The adapter parses with
`"%d-%m-%Y %H:%M:%S"` — matching the **sample**, not the field description. See
§11.9.

### 8.3 EODChartData (daily history)

Official request: `sym` in **`EXCH:TSYM`** format, `from`, `to` (epoch seconds).
Response: `time` (`DD-MMM-YYYY`), `into`/`inth`/`intl`/`intc`, `ssboe`, `intv`.

The adapter builds `formatted_symbol = f"{exchange}:{br_symbol}"` — matching the
documented format — and uses `ssboe` as the timestamp.

**The official EOD response has no `oi` field.** The adapter reads
`candle.get("oi", 0)`, which yields `0`. That is consistent with the code's own
comment (`"oi": 0,  # OI not available in quotes data`) and with the daily-candle
append path.

### 8.4 Index exchange translation

Official `exch` for indices is the **base exchange** (`NSE` / `BSE`), not
`NSE_INDEX`. The adapter rewrites before every call:

```python
if exchange == "NSE_INDEX": exchange = "NSE"
elif exchange == "BSE_INDEX": exchange = "BSE"
```

in `get_quotes`, `get_depth`, `get_history`, and as `api_exchange` in
`_process_quotes_batch`. Matches the official contract.

---

## 9. WebSocket mapping

Endpoint: `wss://piconnect.flattrade.in/PiConnectWSAPI/` — matches
`streaming/flattrade_websocket.py#FlattradeWebSocket.WS_URL` exactly.

### 9.1 Frame types

| Official | Direction | Adapter constant |
|---|---|---|
| `a` | connect | `MSG_TYPE_CONNECT = "a"` |
| `ak` | connect ack | `MSG_TYPE_AUTH_ACK = "ak"` |
| `h` | heartbeat | `MSG_TYPE_HEARTBEAT = "h"` |
| `hk` | heartbeat ack | handled as "internal message" |
| `t` | subscribe touchline | `MSG_TYPE_TOUCHLINE_SUB = "t"` |
| `tk` | touchline ack | `MSG_TOUCHLINE_PARTIAL = "tk"` |
| `tf` | touchline feed | `MSG_TOUCHLINE_FULL = "tf"` |
| `u` | unsubscribe touchline | `MSG_TYPE_TOUCHLINE_UNSUB = "u"` |
| `uk` | unsubscribe ack | ignored |
| `d` | subscribe depth | `MSG_TYPE_DEPTH_SUB = "d"` |
| `dk` | depth ack | `MSG_DEPTH_PARTIAL = "dk"` |
| `df` | depth feed | `MSG_DEPTH_FULL = "df"` |
| `ud` | unsubscribe depth | `MSG_TYPE_DEPTH_UNSUB = "ud"` |
| `udk` | unsubscribe ack | ignored |
| `o` | subscribe order update | `flattrade_order_adapter.py` |
| `om` | order update feed | `flattrade_order_adapter.py` |
| `uo` / `uok` | unsubscribe order update | not sent |
| `p` / `pk` / `pm` | position update | **not implemented** |

### 9.2 Connect payload

| Official | Adapter (`_send_authentication`) |
|---|---|
| `t: "a"` | ✅ |
| `uid` | `self.user_id` |
| `actid` | `self.actid` |
| `source: "API"` | ✅ literal |
| `accesstoken` | `self.accesstoken` |

Exact match.

### 9.3 The `s` value — a real risk

| Source | Value |
|---|---|
| Official connect ack | `"s": Ok / Not_Ok` — **`Ok`** |
| `flattrade_websocket.py#AUTH_SUCCESS` | **`"OK"`** (uppercase) |
| `flattrade_websocket.py#_handle_auth_response` | `if data.get("s") == self.AUTH_SUCCESS` |
| `flattrade_order_adapter.py` | `_noren_text(data.get("s") or data.get("stat")) != "ok"` — **case-insensitive** |

The market-data client compares **case-sensitively against `"OK"`** while the
official docs write **`"Ok"`**. The order adapter already handles both, and its
comment says so explicitly (*"flattrade_websocket.py compares s == 'OK' while the
doc writes 'Ok'; accept either rather than betting on the casing"*).

If Flattrade ever sends `"Ok"`, `_handle_auth_response` takes the failure branch:
sets `auth_failed`, force-closes the socket, and the adapter enters the
auth-retry path — a healthy session treated as rejected. See §11.10.

### 9.4 Subscription key format

Official: `"k": "NSE|22#BSE|508123#…"` — `Exchange|Token` pairs joined with `#`.

The adapter builds exactly that:

```python
scrip = f"{flattrade_exchange}|{token}"          # _create_subscription
ws_client.subscribe_touchline("#".join(touchline_scrips))   # _process_batch_subscriptions
```

This is the same `Exchange|Token` string that issue #1732's scrip-keyed routing
uses internally, which is why the routing fix was a natural fit.

### 9.5 Feed field mapping

Touchline (`tf` / `tk`) → `LTPNormalizer` / `QuoteNormalizer`:

| Official | Adapter key |
|---|---|
| `lp` | `ltp` |
| `v` | `volume` |
| `o`/`h`/`l`/`c` | `open`/`high`/`low`/`close` |
| `ap` | `average_price` |
| `pc` | `percent_change` |
| `ltq` | `last_quantity` |
| `ltt` | `last_trade_time` |
| `ft` | `flattrade_timestamp` |

Depth (`df` / `dk`) → `DepthNormalizer` adds `tbq`→`total_buy_quantity`,
`tsq`→`total_sell_quantity`, the 5-level `bp/bq/bo` + `sp/sq/so` blocks, and
`uc`/`lc`/`52h`/`52l`/`toi`.

Every one of those is a documented field. The normalisers' `safe_float` /
`safe_int` treat `None`, `""` and `"-"` as the default — Noren sends `"-"` for an
absent number, which is why that case is explicit.

### 9.6 Heartbeat

Official: send `{"t":"h"}` every **30 seconds**; ack is `hk` with a timestamp.

`flattrade_websocket.py#HEARTBEAT_INTERVAL = 30` — exact match. The client also
runs `run_forever(ping_interval=30, ping_timeout=10)` and a
`HEARTBEAT_TIMEOUT = 120` data-stall check, so it has three independent
liveness layers.

### 9.7 Order update — the documented handshake, and why it is unused

Official: `{"t":"o","actid":…}`, **no acknowledgement**, feed `om`, unsubscribe
`uo` → `uok`.

`streaming/flattrade_order_adapter.py` implements this exactly, including the
"no ack" quirk and the `pcode`-not-`prd` product field. But
`services/order_update_service.py#_POLLING_BROKERS` contains `"flattrade"`, so
the adapter is **never instantiated** — Flattrade's one-connection-per-uid limit
means a dedicated order socket evicts the market-data socket. See
`07-order-updates.md`.

### 9.8 Position update — not implemented

Official documents `{"t":"p","actid":…}` → `pk`, feed `pm`, carrying `netqty`,
`rpnl`, `buyavgprc`, `sellavgprc`, `child_orders[]`.

The adapter does **not** subscribe to it. Position state comes from polling
`/PositionBook` instead. That is a real capability the official API offers and
the adapter leaves unused — see §10.

---

## 10. Official endpoints the adapter does not use

Each of these is a documented capability with no adapter consumer. Listed so the
gap is explicit rather than discovered later.

| Endpoint | What it offers | Why it matters |
|---|---|---|
| `GetOptionChain` | strikes around a mid price, with `token`/`optt`/`strprc`/`ti`/`ls` | The adapter builds option chains from quotes + the master contract. This endpoint would be one call instead of many. |
| `GetOptionGreek` | call/put price, delta, gamma, theta, rho, vega | OpenAlgo computes greeks itself (`opengreeks`). |
| `GetIndexList` | `{idxname, token}` per index | The adapter derives index tokens from the master contract. |
| `TopListName` / `TopList` | gainers/losers by volume/LTP/value | No OpenAlgo contract for it. |
| `SpanCalc` | span + exposure for a position basket | `GetBasketMargin` is used instead. |
| `GetOrderMargin` | margin for a single order | Not exposed by OpenAlgo. |
| `MultiLegOrderBook` | 2L/3L order legs | Multi-leg price types not exposed. |
| `SingleOrdHist` | full lifecycle per order (`rpt`: New → PendingNew → Open → Fill) | The adapter polls the current book, so intermediate states are lost. |
| `ProductConversion` | CNC ↔ MIS ↔ NRML conversion | No OpenAlgo contract. |
| `ExitSNOOrder` | exit a Cover/Bracket child | Bracket/cover products not exposed. |
| `ExchMsg` / `GetBrokerMsg` | exchange + broker notices | Not surfaced. |
| `GTT` family | good-till-triggered orders | Only `dhan` and `zerodha` ship `api/gtt_api.py`; the capability gate returns 501 for Flattrade. |
| `Postback` webhook | push order updates | `blueprints/postback.py` supports it, but Flattrade is not wired to it. |

---

## 11. Discrepancies found while mapping

Each is a real difference between the official contract and the adapter, read
from both sides. None of these was changed as part of this documentation work —
they are recorded so the decision is visible.

### 11.1 Auth response field name

Official response uses **`status`** (`{ "token", "client", "status": "Ok", "emsg" }`).
`api/auth_api.py#authenticate_broker` checks **`stat`**:

```python
if response_data.get("stat") == "Ok" and "token" in response_data:
```

The `"token" in response_data` half is what actually gates success, so a
`status`-only reply still works — but the `stat` check is against a field the
official response does not document. **Severity: low** (the token check carries
it).

### 11.2 Token rollover window

Official: cleared **5 AM – 6 AM**. Adapter comment and the broker-integration
reference both say **~3 AM IST**. Behaviour is unaffected (the token is re-read
on every reconnect), but the comment is wrong. **Severity: cosmetic.**

### 11.3 `mkt_protection` is not an official field

`transform_data` sends `"mkt_protection": "0"`. It appears in **no** official
PlaceOrder field table. It is either an undocumented field the OMS ignores, or a
leftover from another Noren deployment. **Severity: low** — but it is an
undocumented field on every single order.

### 11.4 `reverse_map_product_type` returns `None` for H / B / F

Official `prd` includes `H` (Cover), `B` (Bracket), `F` (MTF). The reverse map
covers only `C`/`M`/`I`:

```python
reverse_product_type_mapping = {"C": "CNC", "M": "NRML", "I": "MIS"}
return reverse_product_type_mapping.get(product)   # None for H/B/F
```

`close_all_positions` passes that result straight into the square-off payload's
`product`. A position held as Cover or Bracket would therefore send
`product: None`. **Severity: medium** — it cannot happen through the app's own
order path (which only ever sends C/M/I), but it can happen on a position opened
in the Flattrade web terminal.

### 11.5 `EOS` retention is clamped to `DAY`

Official `ret` enum is `DAY` / `EOS` / `IOC`. `_retention` accepts only
`DAY`/`IOC`. A caller sending `EOS` silently gets `DAY`. **Severity: low** — the
engine sends `IOC` or `DAY` only, so nothing in the app path hits it.

### 11.6 `place_order_api` hard-indexes `stat`

```python
if response_data["stat"] == "Ok":
```

A reply without `stat` (an HTML error page, a proxy response) raises `KeyError`
inside the broker module, which the service layer converts to a generic HTTP 500.
Every other book reader in the plugin uses `.get()`. **Severity: medium** — same
defect family as the two documented production bugs, on the order path.

### 11.7 `cancel_order` reads `message`, not `emsg`

Official failure field is `emsg`. The adapter reads
`data.get("message", "Failed to cancel order")`, so the broker's real reason is
dropped and the caller sees the generic string. **Severity: low** — the cancel
still fails correctly, it just reports less.

### 11.8 TradeBook timestamp field

Official TradeBook documents `fltm` (fill time). `transform_tradebook_data` reads
`norentm`, which is documented for **OrderBook** and **SingleOrdHist**, not
TradeBook. **Severity: low** — the tradebook timestamp may render empty.

### 11.9 TPSeries time format — the docs contradict themselves

Official field table: `DD/MM/CCYY hh:mm:ss`. Official sample response:
`"time": "02-06-2020 15:46:23"`. The adapter parses `"%d-%m-%Y %H:%M:%S"`,
matching the sample. If the server ever follows the field description, every
intraday candle is silently dropped (the parse failure is caught and `continue`d).
**Severity: medium** — silent, and it would look like "no history available".

### 11.10 The `s == "OK"` case sensitivity

Covered in §9.3. **Severity: medium** — a healthy session could be treated as an
auth rejection, which force-closes the socket and enters the bounded auth-retry
path.

### 11.11 Order endpoints are not rate-limited

Official: **Order APIs 10/sec, 40/min**; all other APIs 40/sec, 200/min.

`api/data.py` implements the **non-order** limit (38/sec, 190/min) and
`api/funds.py` participates in it. But `api/order_api.py` — PlaceOrder,
ModifyOrder, CancelOrder, and the book reads — **never calls
`_apply_rate_limit()`**. The order path is therefore uncounted against a limit
that is **4× tighter** than the one being enforced. **Severity: medium** — a
square-off sweep over many positions, plus the engine's 3 retries, is exactly the
burst that could hit 10/sec.

---

## 12. Summary — what the mapping proves

1. **The MPP layer is not defensive paranoia.** The official `prctyp` enum is
   `LMT` / `SL-LMT`. `MKT` and `SL-MKT` are not documented values, so the
   rewrite in `transform_data` is the only thing making a MARKET order legal.
2. **The `_read_book` guard is grounded in the documented failure shape.** The
   official PositionBook failure example is literally
   `{"stat":"Not_Ok","emsg":"Error Occurred : 5 \"no data\""}` — the exact string
   `_EMPTY_BOOK_MARKERS` keys on.
3. **The scrip key is the broker's own format.** `Exchange|Token` joined with `#`
   is what the official subscribe frame takes, so issue #1732's routing fix uses
   the broker's native identity rather than an invented one.
4. **The unused endpoints are the real remaining opportunity.** Option chain,
   option greeks, index list, single-order history and position updates are all
   documented, all unused, and each would remove a polling or derivation path.
5. **The order path is the least-guarded part of the plugin.** §11.6 and §11.11
   are both on it, and both are the same family as the two defects that already
   reached production.
