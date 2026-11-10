# Kotak adapter — defect register (fixable vs not)

The Kotak Neo plugin (`broker/kotak/`) did **not** meet the seven engine
requirements that Flattrade meets. The assessment is split into what a code
change can close and what the official API cannot support, in
`broker/kotak/docs/`.

**All 19 fixable defects (K1–K19) are IMPLEMENTED and verified.** Of the 4
not-fixable items, **N1 (historical data) is now RESOLVED** via the Yahoo
Finance fallback ported from PR `marketcalls/openalgo#1466`. N2–N4 remain open —
they need a live Kotak session, not a code change.

K19 (funds PnL) was added later, from PR `marketcalls/openalgo#1224`.

## Read this first
- `broker/kotak/docs/README.md` — requirement mapping (R1–R7), reading order,
  evidence standard.
- `broker/kotak/docs/01-fixable-defects.md` — 18 defects (K1–K18), each with
  code, official-doc evidence, why it matters, and the fix.
- `broker/kotak/docs/02-not-fixable.md` — 4 items (N1–N4) the API cannot
  support, each with what would unblock it.

## Status by requirement
| Req | Before | After | Defects |
|---|---|---|---|
| R1 order placement / MPP / IOC | Partial | **Pass** | K7, K8, K9, K10 |
| R2 square-off cannot oversell | **Fail** | **Pass** | K1, K2, K3, K14 |
| R3 unreadable book ≠ flat book | **Fail** | **Pass** | K4, K13, K15 |
| R4 honest failures | **Fail** | **Pass** | K1, K11, K17, K18 |
| R5 market-data feed | **Pass** | Pass | — |
| R6 funds + load control | Partial | **Pass** | K5, K6, K16 |
| R7 no credentials in logs | **Fail** | **Pass** | K12 |

R1 stops short of a full pass only because historical data has no endpoint at
all (N1) — the order-placement half of R1 is complete.

## What was changed, file by file
| File | Defects |
|---|---|
| `broker/kotak/api/rate_limit.py` (new) | K16 |
| `broker/kotak/api/order_api.py` | K1, K2, K3, K4, K11, K13, K14, K15, K16, K18 |
| `broker/kotak/api/funds.py` | K5, K6, K12, K16, K19 |
| `broker/kotak/mapping/transform_data.py` | K7, K8, K9, K10 |
| `broker/kotak/mapping/order_data.py` | K11 |
| `broker/kotak/mapping/margin_data.py` | K17 |
| `broker/kotak/api/auth_api.py` | K12 |
| `broker/kotak/api/data.py` | K12, K16, **N1 (Yahoo history)** |
| `test/test_kotak_adapter_fixes.py` (new) | verification for K1–K18 |
| `test/test_kotak_history.py` (new) | verification for N1 |
| `test/test_kotak_funds_pnl.py` (new) | verification for K19 |

## Verification
```
.venv/Scripts/python.exe -m pytest test/test_kotak_adapter_fixes.py -q  -> 22 passed
.venv/Scripts/python.exe -m pytest test/test_kotak_history.py -q        -> 20 passed
.venv/Scripts/python.exe -m pytest test/test_kotak_funds_pnl.py -q     -> 15 passed
.venv/Scripts/python.exe -m pytest test/test_smartorder_logic.py -q      -> 204 passed
```
The tests exercise the real functions with the broker/HTTP boundary
monkeypatched, so a regression fails there rather than in production.

## K19 funds PnL — computed from the position book
Kotak's limits reply carries `RealizedMtomPrsnt` / `UnrealizedMtomPrsnt`, but
its `rpnl` is **inaccurate for closed positions** — the reason PR
`marketcalls/openalgo#1224` exists. `api/funds.py#_pnl_from_positions` now
computes both from the position book:
- fully closed (`net_qty == 0`): `sellAmt - buyAmt`
- partial long: `sellAmt - (avgBuy × sellQty)`
- partial short: `(avgSell × buyQty) - buyAmt`
- unrealized: `(ltp - avg) × netQty` — buy-average for longs, sell-average for
  shorts; carry-forward amounts/quantities included throughout
- LTP batch-fetched via `BrokerData.get_multiquotes`, open positions only

**Fallbacks:** the limits reply's own MTOM fields are used when the book is
unreadable, empty, the quote fetch throws, or **not one** open position could be
priced. If *some* priced, the computed figure stands.

**Two deliberate departures from the PR:**
1. `_position_symbol` resolves from `trdSym`/`sym`, not `tok` alone — `tok` is
   not a documented positions field, so the PR's token-only lookup would fail
   every row.
2. `availablecash` stays on the documented `Net` field (K5). The PR's diff
   predates K5 and would have reverted it, reintroducing the silent-zero bug.
   A regression test pins this.

**Collateral fix (post-K19).** The app's P&L is `collateral + m2mrealized +
m2munrealized` (`docs/openalgo-api-contract.md` §2.1). `collateral` mapped from
`CollateralValue`, which equals `Net` on a cash-only account, so P&L displayed
the **balance** as profit (screenshot: P&L +₹2006.59 with M2M 0/0). Now maps
to `Collateral` — Kotak's pledged-SECURITIES field, the same meaning as
Flattrade's `brkcollamt`. `availablecash` still carries the balance for the BAL
pill. Live verified: `availablecash=2006.59`, `collateral=0.00`.

R5 is the one area where Kotak is **better** than Flattrade: the order-update
adapter has a connect-ack watchdog + handshake-encoding alternation, and the
market-data adapter has exponential-backoff reconnect, debounced batch
subscribe and partial-update merging. Flattrade polls REST for order updates.

## The four that could move money — now fixed
- **K1** `api/order_api.py#close_all_positions` now honours the reply: success
  only when HTTP 200 **and** `stat == "Ok"`; a rejection returns 500 with the
  failing symbols and `emsg` instead of a false "SquaredOff".
- **K2** the three Flattrade guards are ported: per-account lock
  (`_get_close_lock`), working-SELL ledger (`_working_sell_orders`), oversell
  guard. The ledger subtracts the trade book's `fldQty` per `nOrdNo` because the
  Kotak order book carries **no** fill-quantity field.
- **K3** `_resolve_position_symbol` resolves from `trdSym`/`sym` (via
  `get_oa_symbol`), using `tok` only when the broker happens to include it — no
  more KeyError aborting the sweep.
- **K4** `_read_book` + `BrokerReadError` distinguish empty from unreadable;
  `get_open_position` propagates the error so a smart order never trades on a
  book it could not see. Kotak's empty marker differs from Flattrade's: `stat:
  "Ok"` with an empty/absent `data`, not a `"no data"` emsg.

## N1 historical data — RESOLVED (Yahoo fallback)
Kotak Neo has **no candle endpoint** (only scrip-master CSV links and a current
quote snapshot), so `api/data.py#get_history` used to return an empty frame and
`services/history_service.py#get_history_with_auth` reported it as `success`
with zero rows — charts rendered empty.

Ported from PR `marketcalls/openalgo#1466`. `api/data.py#get_history` now sources
candles from the free Yahoo Finance chart API:
- `timeframe_map` — `1m/5m/15m/30m/1h/D` → Yahoo `1m/5m/15m/30m/60m/1d`.
  `services/intervals_service.py#get_intervals_with_auth` derives `/intervals`
  from this map, so the map is what makes that endpoint work.
- `_yahoo_ticker` — `NSE`→`.NS`, `BSE`→`.BO`, plus `NIFTY`→`^NSEI`,
  `BANKNIFTY`→`^NSEBANK`, `SENSEX`→`^BSESN`. Returns `None` for F&O/currency/
  commodities so an option leg degrades to empty rather than fetching the wrong
  instrument.
- `_ist_midnight_epoch` — IST-midnight epoch boundaries, `end=True` returns the
  next day so the end date is inclusive.
- `get_history` skips null gap candles, de-dupes, sorts and clamps to the window.

**Verified live:** `NIFTY` → 6 daily candles, `RELIANCE` → 6 daily candles,
`NIFTY26NOV25000CE` → empty (no request issued). Tests:
`test/test_kotak_history.py` (20 passing).

**Residual gap:** Yahoo carries no Indian F&O, so option-leg history is still
unavailable — the OI/IV/straddle charts that need legs still degrade to empty.
Historify backfill (`get_history(source="db")`) remains the answer for those.

## N2 open interest — still open
Looks like data, not an error, which is why it is documented rather than left as
a known limitation.
- **N2 open interest** — `05-market-data-quotes.md` lists `oi` as a **filter**
  you can request, never as a response **field**. `api/data.py#get_quotes` reads
  `open_int`, a guess. Every OI is silently 0, so
  `services/oi_profile_service.py#_fetch_daily_oi_changes` filters to nothing
  and the OI chain renders all-zero. Unblock: one live authenticated quotes
  call to read the real key.

## N3 — SL-M is NOT a defect
The doc permits `pt: "SL-M"`; live Kotak rejected it ("Market order with Algo Id
not allowed", order 260728000251866). The doc cannot settle the contradiction.
`mapping/transform_data.py#_apply_slm_conversion` is correct under **both**
readings — a converted SL still rests and triggers at the same price, and an
unconverted SL-M would be dead on arrival. Do not "fix" it back to SL-M.

## The rule that generalises
Same as Flattrade: a broker reply is **not always** the documented success
payload. On failure it is the error object — a bare dict with `stat: "Not_Ok"` +
`emsg` and **no** `data`. Indexing or iterating it before type-checking raises,
and the service layer reports that as a failed trading operation. Kotak had
five such hard-index sites (K11), all now guarded.

Two spellings to keep straight: the **order/portfolio** endpoints use `emsg`
(`errMsg` belongs to the login family), and the failure status is `"Not_Ok"`
with the underscore — `"NotOk"` is invisible to any caller testing the
documented value.

Corollary: **distinguish "empty" from "unreadable" explicitly.** An empty book
is a fact; an unreadable book is an unknown. Trade on the first, never the
second.

## Upgrade risk
`broker/kotak/` is vendored upstream code, same as Flattrade. Every fix is a
**local patch** — an OpenAlgo upgrade that overwrites these files silently
reintroduces the defects.

See [[gotchas]], [[features/flattrade-adapter]] for the reference
implementation of each guard, and [[features/broker-api-docs]] for the mirrored
Kotak docs.
