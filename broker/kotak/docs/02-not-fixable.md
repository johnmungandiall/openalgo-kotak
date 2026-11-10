# Kotak adapter — not fixable from the official API

4 items that could not be closed by a code change against the official Kotak
Neo Trade API v2 documentation, because it does not provide what the fix would
need — or contradicts the behaviour the code depends on.

Each entry states what is missing, what was checked to establish that it is
missing, what the consequence is, and what would unblock it. "Not fixable" here
means **not fixable from the documentation**, not "impossible forever" — and
that distinction has already paid off: **N1 is now resolved** by sourcing
candles from outside the broker (PR #1466), which is exactly the kind of
unblock this document was written to name.

**The 18 fixable defects are implemented** (see
[`01-fixable-defects.md`](01-fixable-defects.md)). Of the 4 items below, N1 is
resolved; N2–N4 remain open because each needs a live Kotak session or an
account setting, not a code change.

## Index

| ID | Item | Req | Blocked by | Status |
| --- | --- | --- | --- | --- |
| N1 | Historical data — `get_history` returns an empty DataFrame | R1 | No candle endpoint in the API | **RESOLVED** — Yahoo fallback (PR #1466) |
| N2 | Open interest — the response field name is undocumented | R1 | Doc lists `oi` as a filter, never as a field | Open |
| N3 | SL-M — the doc permits it, live Kotak rejects it | R1 | Doc and live behaviour contradict | Open (not a defect) |
| N4 | Static IP whitelisting | — | Operational, not code | Open |

---

## N1 — Historical data: RESOLVED via the Yahoo Finance fallback

**Req:** R1 · **Was blocked by:** the API has no historical-candle endpoint

> **Status: resolved.** Implemented from PR
> [`marketcalls/openalgo#1466`](https://github.com/marketcalls/openalgo/pull/1466).
> `broker/kotak/api/data.py#get_history` now sources candles from the free Yahoo
> Finance chart API for **indices and cash equities**. Option/future legs still
> return an empty frame — Yahoo does not carry Indian F&O — so the OI/IV/straddle
> charts that need option legs remain unavailable. Verified live: `NIFTY` → 6
> daily candles, `RELIANCE` → 6 daily candles, `NIFTY26NOV25000CE` → empty.
> Tests: `test/test_kotak_history.py` (20 passing).
>
> The original analysis is kept below because it explains *why* a fallback was
> needed and what the remaining gap is.

### Original analysis (kept for context)

**Code.** `broker/kotak/api/data.py#get_history`

```python
def get_history(self, symbol, exchange, interval, start_date, end_date) -> pd.DataFrame:
    """Placeholder for historical data - not supported by Kotak Neo"""
    empty_df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    logger.warning("Kotak Neo does not support historical data")
    return empty_df
```

`broker/kotak/api/data.py#get_supported_intervals` returns every interval list
empty, and `BrokerData.__init__` sets `self.timeframe_map = {}`.

**What was checked.** All 16 files in `broker-api-docs/kotak-api-docs/` were
read. The market-data surface is exactly two endpoints:

- `04-market-data-instruments.md` — `GET /script-details/1.0/masterscrip/file-paths`,
  which returns **download links to CSV scrip-master files**. Instrument
  metadata, not prices.
- `05-market-data-quotes.md` — `GET /script-details/1.0/quotes/neosymbol/<query>[/<filter>]`,
  which returns a **current snapshot**: `ltp`, `ohlc` (today's open/high/low
  and previous close), `depth`, `last_volume`, `total_buy`, `total_sell`,
  `year_high`, `year_low`.

There is no endpoint that returns a series of candles over a date range. The
`ohlc` object in the quotes response is one day's four numbers, not history.
`12-neo-websocket.md` and `13-order-position-streaming-websocket.md` are live
streams, not backfill.

**Consequence.** Every engine feature that reads history is dead for a Kotak
account. The callers are not marginal:

| Caller | What it needs history for |
| --- | --- |
| `services/strategy_chart_service.py` | underlying series + per-leg series for the combined-premium chart |
| `services/iv_chart_service.py` | underlying + CE + PE series to compute implied volatility |
| `services/oi_profile_service.py#_fetch_daily_oi_changes` | previous day's OI per strike |
| `services/straddle_chart_service.py` | both legs |
| `services/custom_straddle_service.py` | both legs |
| `services/multi_strike_oi_service.py` | multiple strikes |
| `services/indicator_service.py` | any indicator |
| `blueprints/pnltracker.py` | P&L chart series |
| `blueprints/scalping.py` | scalping chart |
| `portfolio/data.py` | backtester price series |

`services/history_service.py#get_history_with_auth` calls
`data_handler.get_history(...)`, gets an empty DataFrame back, and returns
`{"status": "success", "data": []}` — **success with no rows**. So the failure
is silent: the chart renders empty rather than reporting that the broker cannot
supply history.

**Why a code change cannot fix it.** There is nothing to call. Any fix must
introduce a data source that is not Kotak.

**What unblocked it — option 2 was taken**

1. **Historify backfill (already in the codebase, no new dependency).**
   `services/history_service.py#get_history` already accepts `source="db"` and
   routes to `get_history_from_db`, which reads local DuckDB via
   `database/historify_db.py#get_ohlcv`. `services/historify_service.py#download_data`
   populates that store — but it populates it by calling `get_history`, i.e.
   from a broker that *has* history. So the flow is: backfill with a
   history-capable broker (Flattrade has `broker/flattrade/api/data.py#get_history`),
   then serve Kotak users from DuckDB. This is the architecturally intended
   path — the `source` parameter exists for exactly this — and it adds no
   external dependency. It requires the user to have a second broker session
   for the backfill, or a shared store.
2. **Yahoo Finance as an equity/index-only fallback.** Verified live during
   this assessment: `RELIANCE.NS` and `^NSEI` both return daily candles with
   open/high/low/close/volume. But `NIFTY26NOV25000CE.NS` returns **HTTP 404** —
   Indian options and futures are not covered. Since most of the callers above
   need option legs, this covers only the underlying series. It also needs a
   symbol-mapping layer (OpenAlgo `NIFTY` → Yahoo `^NSEI`, `RELIANCE` →
   `RELIANCE.NS`) with NSE/BSE disambiguation, and it cannot supply OI (see
   N2). `yfinance` is **not** a production dependency — `pyproject.toml` keeps
   it in the opt-in `analysis` group with the comment "so
   streamlit/yfinance/seaborn never reach a production install" — so this would
   be a direct `httpx`/`requests` call, both of which are already dependencies.
3. **Ask Kotak.** The v2 docs are a migration guide; a historical endpoint may
   exist outside the mirrored set. This is the only option that would make
   `get_history` genuinely work, and it is not something code can settle.

**What was actually chosen.** Option 2, per the user's instruction to follow PR
#1466. The PR's own scoping is what keeps it honest: `_yahoo_ticker` returns
`None` for anything Yahoo does not carry, so an option leg degrades to an empty
frame instead of silently returning the underlying's prices under an option's
name. The residual gap is real and documented above — option-leg history is
still unavailable.

Option 1 (Historify backfill) remains the better long-term answer for option
legs, and the two are not exclusive: the Yahoo fallback covers the underlying
series today, and a backfill would cover the legs.

---

## N2 — Open interest: the response field name is undocumented

**Req:** R1 · **Blocked by:** the doc names `oi` as a filter, never as a field

**Code.** `broker/kotak/api/data.py#get_quotes` and
`broker/kotak/api/data.py#_process_quotes_batch` both read:

```python
"oi": int(quote_data.get("open_int", 0)),
```

`open_int` is a guess. Nothing in the code explains where the name came from.

**What was checked.** `broker-api-docs/kotak-api-docs/05-market-data-quotes.md`
§4 lists the eight allowed `filter_name` values: `all`, `52W`,
`scrip_details`, `circuit_limits`, `ohlc`, `oi`, `depth`, `ltp`. So `oi` is a
**filter you can request**.

§5 then gives the response field table: `exchange_token`, `display_symbol`,
`exchange`, `lstup_time`, `ltp`, `last_traded_quantity`, `total_buy`,
`total_sell`, `last_volume`, `change`, `per_change`, `year_high`, `year_low`,
`ohlc`, `depth`. **No open-interest field is listed**, and the worked example
response contains none.

So the doc confirms you can ask for OI and never says what comes back.

**Consequence.** `.get("open_int", 0)` returns `0` on every quote. The failure
is completely silent — no exception, no warning, a plausible-looking zero.

Downstream, `services/oi_profile_service.py#get_oi_profile_data` builds the OI
chain from `ce.get("oi", 0)` and `pe.get("oi", 0)`, and
`#_fetch_daily_oi_changes` filters to `s.get("oi", 0) > 0`. With every OI zero,
that filter selects **nothing**, so no history is fetched and every
`ce_oi_change`/`pe_oi_change` stays at its initialised `0`. The OI profile
renders a flat, all-zero chain that looks like a real reading of a market with
no open interest.

**Why a code change cannot fix it.** The correct field name is unknown. Any
name written now is a second guess, and a wrong guess fails the same silent
way.

**What would unblock it.** One live authenticated call to
`/script-details/1.0/quotes/neosymbol/nse_cm|<pSymbol>/oi` and reading the
actual key. That is a five-minute check with a real session — it cannot be done
from the docs, and it cannot be done here because no Kotak credentials are
configured. Until then the honest state is "unknown", not "zero".

**Interim mitigation, if the OI profile must ship before that check.** Make the
absence visible: if no OI key is found in the response, log a warning once per
session and have the OI profile report OI as unavailable rather than as zero.
A wrong number presented as data is worse than a stated gap.

---

## N3 — SL-M: the doc permits it, live Kotak rejects it

**Req:** R1 · **Blocked by:** the doc and observed behaviour contradict

**Code.** `broker/kotak/mapping/transform_data.py#_apply_slm_conversion`
rewrites every SL-M into an SL with a protective limit price, and
`#_slm_protected_price` derives that price from the trigger, one MPP band
beyond it in the fill direction, snapped to the exchange tick and forced at
least one tick past the trigger. It fails closed when the tick size cannot be
resolved.

The code's own comment records why:

> Kotak rejects SL-M from API sessions outright: "Market order with Algo Id not
> allowed" (observed live 2026-07-28, order 260728000251866). Per SEBI, market
> orders are not permitted for retail algos, and Kotak's APIs append an
> exchange-compliant Algo ID to every order automatically.

**What was checked.** `broker-api-docs/kotak-api-docs/06-trading-place-modify-cancel.md`
§4 lists `pt` — "Order type", allowed `L` (Limit), `MKT` (Market), `SL`
(Stoploss), **`SL-M` (SL-Market)**. The modify table lists the same four.

So the official doc says SL-M is a valid order type. Live Kotak rejected it
with an explicit error naming the Algo ID.

**Why this is not fixable from the doc.** The doc cannot settle which is true.
It is not a documentation gap that more reading would close — the doc states
one thing and the broker did another. Resolving it needs either a fresh live
test (to see whether the rejection still happens, and whether it is
account-specific or universal) or a statement from Kotak.

**Why the current code is nonetheless correct.** The conversion is the safe
side of the contradiction. If SL-M is rejected, an unconverted SL-M is an order
that is dead on arrival while the caller believes the stop is resting — which
is how a position ends up unprotected. If SL-M were in fact accepted, the
converted SL still rests and still triggers at the same price; it is simply a
stop-limit rather than a stop-market, with the limit placed one MPP band beyond
the trigger so it stays marketable once triggered. The conversion is therefore
correct under both readings, which is why it is **not** listed as a defect.

**What would unblock a definitive answer.** A live SL-M placement on a test
account, recording the exact reply. Until then the conversion stays, and the
comment in `_slm_protected_price` is the record of why.

**One thing to watch.** If Kotak later starts accepting SL-M, the conversion
becomes unnecessary but harmless. If Kotak instead starts rejecting `SL` with a
protective limit for some instrument class, the conversion would need revisiting
— so the post-upgrade checklist should include one SL-M placement.

---

## N4 — Static IP whitelisting

**Req:** — · **Blocked by:** it is an account setting, not code

**What the doc says.**
`broker-api-docs/kotak-api-docs/03-static-ip-whitelisting.md` and
`14-troubleshooting-faqs-error-codes.md` Q13–Q18: the API dashboard accepts one
primary and one secondary IP, each changeable once per week, with up to 10
family accounts linkable to the same whitelisted IP. Q15: "For SEBI compliance
and security… Optional now, but will become mandatory once the circular is
effective."

**Why it is listed.** It is not a defect and there is nothing to fix in
`broker/kotak/`. It is here because it is the one remaining item that will
break a working Kotak integration without any code changing, and it belongs in
the same register so it is not forgotten.

**Consequence when it becomes mandatory.** Every API call from an
un-whitelisted IP fails at the gateway. The adapter's error handling would
surface it as a generic failure — which is why K11 (guarding the reply shapes)
matters: without it, an IP rejection and a session expiry and a code fault all
look the same.

**What would unblock it.** Nothing in code. The user registers the IP on the
NEO API dashboard. The only code-side work is making the failure legible, which
is K11.

---

## Summary — what "not fixable" actually means here

| ID | Permanently blocked? | Unblock |
| --- | --- | --- |
| N1 | No | **DONE** — Yahoo fallback (PR #1466) for indices + cash equities; option legs still need Historify backfill |
| N2 | No | One live authenticated quotes call to read the real OI key |
| N3 | No | One live SL-M placement to settle doc-vs-broker |
| N4 | No | User registers the IP on the dashboard |

None of the four is a dead end. Three need a live Kotak session, which is not
available in this environment — that is the actual blocker, not the API. The
fourth (N1) has a working in-repo answer that does not need Kotak at all.

The distinction that matters for planning: **N1 and N2 are silent wrong
answers**, not errors. A chart that renders empty and an OI chain that reads
zero both look like data. That is why they are documented here rather than left
as known limitations — a user who knows the feature is unavailable behaves
differently from one who trusts a zero.

## Related

- [`01-fixable-defects.md`](01-fixable-defects.md) — the 19 defects that a code
  change can close.
- [`README.md`](README.md) — the requirement mapping and reading order.
- `broker/flattrade/docs/10-official-api-mapping.md` §11 — the same exercise for
  Flattrade, including the discrepancies that *are* fixable there.
- `services/history_service.py#get_history` — the `source="db"` path that N1
  option 1 relies on.
