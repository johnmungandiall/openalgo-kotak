---
name: broker-engine-audit
description: Audit an EXISTING OpenAlgo broker plugin against the Qwantonomous Engine's seven hard requirements (R1–R7) and fix every fixable defect. Use when asked whether broker X "satisfies the engine requirements", to bring another broker up to Flattrade's standard, to fix a broker's square-off / funds / history / options-chain / credential-logging defects, or to catalogue a plugin's defects as fixable vs not-fixable. Not for wiring a brand-new broker — use broker-integration for that.
---

# Auditing an existing broker plugin against the engine

> **Golden rule:** the plugin already exists and mostly works. Your job is not to
> rewrite it — it is to find every place where its *failure shape* leaks into the
> engine as a *success shape*, then close it. Fix the plugin, never the engine.

Distinct from `broker-integration` (which wires a NEW broker from a family
template). This skill is the **second pass**: an integration that "works" on the
happy path is audited against R1–R7, and every defect class below is checked for
in that broker's own code and its own official docs.

**Reference implementation: `broker/flattrade/`.** It has been audited and fixed;
its docs (`broker/flattrade/docs/`, 11 files) are the template for the register
you will write. The audit record for Kotak is `broker/kotak/docs/` (K1–K19).

---

## 0. What "done" means

A broker is done only when ALL of these hold:

1. Every defect has a **code citation** (`path#symbol`) AND an **official-doc
   citation** (`broker-api-docs/<broker>-api-docs/NN-*.md`). No claim from memory.
2. Each defect is classified **fixable** or **not-fixable**, and a not-fixable
   one names what would unblock it (a live session, a different data source, or
   an operational action).
3. Fixes are **implemented**, not just listed.
4. A hermetic test per defect family passes, and the existing suite still passes.
5. The **live** endpoints are probed against the running server: order path
   shape, funds values, options chain legs non-zero, history rows.
6. `broker/<name>/docs/` holds the register, and `kb/features/<name>-adapter.md`
   indexes it.
7. One commit carries code + docs + kb note together.

---

## 1. The requirement frame — R1–R7 (do not re-invent these)

The engine is a strategy client; it never talks to the broker directly. Each
requirement below, when unmet, produces a user-visible trading defect. Copy this
table into the audit doc and mark each row before/after.

| # | Requirement | What breaks when unmet |
|---|---|---|
| **R1** | A `MARKET` order must actually reach the exchange | Broker rejects the price type; engine believes it holds a position it does not |
| **R2** | A square-off must never take the account through zero | Two sweeps read the same stale qty and the second sells a long twice → **short**, unlimited risk |
| **R3** | "Nothing to do" must not look like a failure | Flat account → HTTP 500 → engine retries 3× and holds its side switch in "closing" |
| **R4** | A failure must not look like success | Plugin says "All Open Positions SquaredOff" while placing no order; engine stops trying, position stays open |
| **R5** | A price feed must survive a drop without lying | Socket "connected but silent" → engine computes PnL/trailing/exits against a frozen price |
| **R6** | A balance read must not cost 2 round-trips per poll | Funds polls several times a second and starves the shared order-rate budget |
| **R7** | A credential must never appear in a log or a cache key | Live session token/sid in debug logs, or as a plain cache key |

**R4 is the dangerous one.** Weigh every finding by "what does the engine believe
after this?" — not by how big the diff is.

---

## 2. Get the broker's official docs BEFORE reading code

Never audit against memory of "how broker APIs usually look". Two sources, in
this order:

1. **The local mirror** — `broker-api-docs/<broker>-api-docs/`. Check it exists:
   ```bash
   ls broker-api-docs/
   ```
   Plugin docstrings already cite these paths (e.g.
   `broker/kotak/streaming/kotak_order_adapter.py` → `kotak-api-docs/13-order-position-streaming-websocket.md`),
   so a missing mirror means the citations are unresolvable — grab it.

2. **Fetch the mirror** from `marketcalls/broker-api-docs` (`master`), same repo
   the docstrings cite. List, then download the whole folder:
   ```powershell
   # list
   Invoke-RestMethod 'https://api.github.com/repos/marketcalls/broker-api-docs/contents/<broker>-api-docs' |
     Select-Object -ExpandProperty name
   # download each
   Invoke-WebRequest "https://raw.githubusercontent.com/marketcalls/broker-api-docs/master/<broker>-api-docs/<file>" -OutFile ...
   ```
   Then **verify** each file is real markdown: non-empty, starts with `#`, no
   `<html>` / `404` body, last line not truncated mid-sentence.

Record in the docs that this mirror is an **unofficial conversion** ("for
personal/educational reference") — a strong secondary source, not the vendor's
signed contract. If the vendor portal is reachable, prefer it.

---

## 3. Read the plugin end-to-end, in this file order

Read with `read_file`; the audit is worthless if it is second-hand.

| Order | File | What you are looking for |
|---|---|---|
| 1 | `plugin.json` | `supported_exchanges` — anything outside it is a mapping gap |
| 2 | `api/order_api.py` | `place_order_api`, `modify`, `cancel`, `get_open_position`, `close_all_positions`, `cancel_all_orders_api`, `place_smartorder_api` — **every** broker reply read |
| 3 | `mapping/transform_data.py` | pricetype / product / action / retention enums, MPP emulation, tick rounding |
| 4 | `mapping/order_data.py` | book row → common shape, status normalisation, statistics |
| 5 | `api/funds.py` | field names against the doc, cache, PnL source |
| 6 | `api/data.py` | `get_quotes`, `get_multiquotes`, `get_depth`, `get_history`, `get_supported_intervals`, batch size |
| 7 | `api/auth_api.py` | token shape, what is logged |
| 8 | `mapping/margin_data.py` | margin/basket order path, error field names |
| 9 | `streaming/*` | feed lifecycle: watchdog, reconnect ceiling, token refresh, subscribe-after-auth |
| 10 | `database/master_contract_db.py` | symbol normalisation, tick defaults, expiry format, escaping |

**Grep for the reply-shape defect first** — it is present in nearly every plugin:

```
getattr|\.get\(|\bresponse_data\[|\bdata\[0\]|\beres\.json\(\)|\bstat\b
```

---

## 4. The defect taxonomy — check every family for every broker

These families recurred across Flattrade and Kotak; expect them everywhere.

### F1 — Reply shape (the root family)
A broker reply is **not always** the documented success payload. On failure it is
an error object. Sub-shapes seen:

| Sub-shape | Symptom |
|---|---|
| bare `{"stat":"Not_Ok","emsg":…}` where a LIST was expected | `KeyError: 0` / `IndexError` |
| bare dict where the code does `for x in reply: x.get(...)` | `AttributeError: 'str' object has no attribute 'get'` (iterating the dict's keys) |
| `None` for "no rows" | `TypeError: 'NoneType' is not subscriptable` |
| HTML error page / proxy body | `json.JSONDecodeError` → generic 500 |
| numeric fields as **strings** | `float()`/`int()` raise, or silent `0` |
| reply dict renamed per endpoint (`data` vs `values` vs bare list) | hard index misses |

Rule: a reader returns rows or **raises a typed read error**; it never returns
`[]` for "could not read". Distinguish **empty** (flat account — a valid answer)
from **unreadable** (raise). Kotak `K4` and Flattrade `BrokerReadError` are the
worked example.

### F2 — Square-off safety (four guards, all mandatory)
1. **Read-vs-empty** — unreadable book must raise, not read as flat (F1).
2. **Per-account lock** — two concurrent sweeps must not both read the same qty.
3. **Working-SELL ledger** — count sells already placed this sweep/retry. Derive
   remaining qty from the order book (`open`/`trigger pending` sells **and**
   trade-book fills), because most order books carry **no filled-quantity
   field** — Kotak needed `fldQty` joined on `nOrdNo`.
4. **Oversell guard** — closing qty can never exceed the long.
Plus: **honest report** — success only when the broker replied success for every
leg; otherwise a non-2xx with the failing symbols named.

### F3 — Funds
- **Field names must exist in the doc.** Kotak summed `RmsPayInAmt +
  RmsPayOutAmt + Collateral`, none documented → all silently 0. The documented
  `Net` was correct. A name not in the doc is a **guess** and fails silently.
- **`collateral` is pledged securities, not the balance.** The engine's PnL is
  `collateral + m2mrealized + m2munrealized`, so mapping the cash balance into
  `collateral` shows the whole balance as profit. Flattrade uses `brkcollamt`;
  Kotak uses `CollateralValue`; `Collateral` was `0`.
- **PnL source**: some brokers' own realized figure is wrong for closed
  positions (Kotak `rpnl` — PR #1224). Compute realized/unrealized from the
  **position book**, with the limits reply's MTOM as fallback when the book is
  unreadable or quotes are missing.
- **Cache + single-flight** (R6): TTL cache keyed by a **hashed** account key,
  one in-flight fetch per key so N concurrent polls issue one broker call.
- **Never return `{}` for a failure** without the caller knowing — that becomes
  `200 success + empty data` at the service layer.

### F4 — Order mapping
- Pricetype: normalise the enum; never emit an undocumented literal as a default
  (Kotak `map_order_type` defaulted to `"MARKET"`, valid values are `L/MKT/SL/SL-M`).
- **Emulate MARKET only when the broker refuses it.** Verify against the doc
  *and* a live order: Flattrade's doc lists only `LMT/SL-LMT` (so conversion is
  mandatory), Kotak's lists `MKT` (so conversion is a live-behaviour decision,
  not a doc one). Convert MARKET→LMT and SL-M→SL-**LMT priced off the trigger**,
  never the LTP, and **fail loudly** when no reference price is available.
- Product: map every documented value (`CNC/NRML/MIS/MTF/CO/BO`); a member
  missing from the reverse map becomes `product: null` on square-off.
- Retention: `DAY`/`IOC` (and `EOS` where documented) must be reachable — fields
  are usually `rt` on place and a **different** name on modify (`vd`).

### F5 — Credentials in logs
Grep `logger\.(debug|info)` for `token`, `sid`, `response.text`, `[:10]`,
`Authorization`. Log the **shape** ("token present, 64 chars"), never a prefix —
Flattrade hashes the account key for the cache; every broker should.

### F6 — Rate limiter
Implement the **documented** limit as an account-wide sliding window and apply it
to every outbound call, then wire it into the order path specifically (a limiter
that exists but is never called is a defect). Kotak/Flattrade order limits are
much tighter than the generic data limit (10/s · 40/min) — check the doc.

### F7 — Options chain / quote batch
The engine's chain needs per-leg `symbol` + non-zero `ltp` + non-zero `oi`; it
**skips zeros** as untradeable. So:
- Every leg row must carry the canonical key set: `symbol, label, ltp, bid, ask,
  bid_qty, ask_qty, open, high, low, prev_close, volume, oi, lotsize, tick_size`.
- `tick_size` comes from the symbol master, not the quote — if the multiquote
  dict omits it the chain renders tick 0.
- **Respect the broker's per-request symbol cap.** A full chain asks for 2×(2·N+1)
  symbols (N=15 → 62). Kotak rejects >50 per `neosymbol` call → whole leg set
  came back zero. Chunk **and** verify each chunk.
- **Verify the loop, not the call.** A parse/append block indented *outside* the
  per-query loop silently returns 1 result for 22 symbols. Probe: N symbols in →
  N results out.

### F8 — History
No candle endpoint is common (Kotak Neo v2 has none). Options: a free fallback
for **indices + cash equity only** (Yahoo chart API — see PR #1466 in this tree),
with option/future legs returning an empty frame *without calling out*; or the
in-repo Historify path (`get_history(source="db")`) after a backfill from a
broker that does have history. State which, and why the residual gap remains.

### F9 — Feed lifecycle
Watchdog for silent-but-open sockets, a reconnect ceiling the engine can live
with, fresh token from the DB on reconnect (not the cached one), subscribe only
**after** auth ack, close-before-reconnect, and FD hygiene.

---

## 5. Classify: fixable vs not-fixable (write two documents)

`docs/01-fixable-defects.md` — one section per defect: **symptom → code cite →
doc cite → why dangerous → fix → test**. Number them (`K1…Kn`) so the register
can be referenced.

`docs/02-not-fixable.md` — only four legitimate classes:

| Class | Example | Unblock |
|---|---|---|
| No endpoint at all | Kotak has no candle API | third-party fallback or backfill |
| Field name undocumented | doc lists `oi` as a *filter*, never a response field | one live call to read the real key |
| Doc contradicts live | doc permits `SL-M`; live rejects it | one live order to settle |
| Operational, not code | SEBI static-IP whitelisting | user registers the IP |

Say plainly: "not fixable **from the documentation**", not "impossible". Every
entry names its unblock.

---

## 6. Implementation order (cheapest-risk first)

1. `api/rate_limit.py` — new, self-contained, no callers yet.
2. `api/order_api.py` — read-vs-empty helper (`BrokerReadError`), square-off
   rewrite, cancel-all guards, flip clamp, cache invalidation, guarded indexes.
3. `mapping/transform_data.py` + `mapping/order_data.py` — enums, retention,
   status normalisation, statistics guards.
4. `api/funds.py` — documented field names, position-book PnL, cache +
   single-flight.
5. `api/auth_api.py` + `api/data.py` — strip credential logging, add limiter,
   fix quote batching / `tick_size`.
6. `mapping/margin_data.py` + `database/master_contract_db.py` — error field
   names, symbol normalisation.

Keep every edit **targeted**. Never run a whole-file formatter on a large legacy
source file — a 3-line change becomes a 2000-line diff and the real change is
lost in review.

---

## 7. Tests — one hermetic test per family

`test/test_<broker>_<area>.py`, no network, broker transport replaced by a fake:

- reply-shape matrix: `None`, `[]`, error dict, HTML body, string-typed numbers
- square-off: flat book, unreadable book, concurrent sweep, existing working
  SELL, oversell attempt, rejected close
- funds: documented fields present, a documented field missing, PnL from book,
  PnL fallback when the book is unreadable, cache hit does one call
- order mapping: every documented pricetype/product/retention, unknown → raises
- chain: N symbols in → N results out, all `ltp > 0`, `tick_size` present

Run the project's own runner (this tree: `.venv\Scripts\python.exe -m pytest`),
targeted first, then the neighbouring suites to prove no regression.

---

## 8. Live verification — mandatory, not optional

Restart the server yourself so the new code is loaded (`start.bat` in this tree
points at a stale folder — start with `.venv\Scripts\python.exe app.py` in the
background, then confirm the port is listening).

Then probe with the engine's own request shape:

```bash
curl -X POST http://127.0.0.1:5001/api/v1/funds -H "Content-Type: application/json" -d '{"apikey":"…"}'
curl -X POST http://127.0.0.1:5001/api/v1/optionchain -d '{"apikey":"…","underlying":"NIFTY","exchange":"NFO","expiry_date":"17NOV26","strike_count":15}'
curl -X POST http://127.0.0.1:5001/api/v1/history -d '{"apikey":"…","symbol":"NIFTY","exchange":"NSE_INDEX","interval":"D","start_date":"2026-11-01","end_date":"2026-11-10"}'
```

Green looks like: funds carries the documented numbers (collateral **not** equal
to the cash balance), the chain has `legs = 2 × strikes` with **all** `ltp > 0`
and `oi > 0`, history returns rows, and a flat account answers "nothing to do"
with 200, never 500.

Watch the server log during the probe — a swallowed exception there is the defect
reappearing under a new name.

---

## 9. Document and commit

- `broker/<name>/docs/README.md` — requirement table before/after, reading order,
  evidence standard, the file-by-file change list.
- `broker/<name>/docs/01-fixable-defects.md`, `02-not-fixable.md`.
- `kb/features/<name>-adapter.md` — index note with the status table and the
  defects-per-requirement mapping.
- One commit, code **and** docs **and** kb note together. Use a pathspec if the
  index holds another task's files; verify with `git show --stat HEAD`.

---

## Traps already paid for

- **An upstream PR may predate your own fixes.** PR #1224's `funds.py` hunk was
  written against the pre-fix file: applying it verbatim would have reverted the
  verified `Net` field back to the silent-zero formula. Take the PR's
  **substance**, re-express it on the current file, and pin the old fix with a
  test.
- **A field being undocumented is not proof it is absent.** `open_int` was a
  guess that turned out correct on a live call — but it was only *verified* by
  calling, never by the doc. Label the evidence level.
- **The chain's zero legs are usually a request-side failure**, not a quote
  problem: check the per-request symbol cap and the loop indentation before
  touching pricing.
- **Server restarts are part of verification.** Code that compiles and passes
  unit tests is not loaded until the process is restarted.

---

## Per-broker checklist (copy into `plan.md`)

```
[ ] official docs mirrored at broker-api-docs/<broker>-api-docs/ and verified
[ ] F1  reply-shape: every read guarded, empty vs unreadable separated
[ ] F2  square-off: read-vs-empty, lock, working-sell ledger, oversell guard,
       honest per-leg report
[ ] F3  funds: documented field names, collateral ≠ balance, PnL source,
       TTL cache + single-flight
[ ] F4  order mapping: pricetype, MARKET/SL-M emulation, product, retention
[ ] F5  no token/sid/response body in any log line
[ ] F6  documented rate limit implemented AND called on the order path
[ ] F7  chain: canonical leg keys, tick_size, per-request cap chunked,
       N-in → N-out verified
[ ] F8  history: endpoint or documented fallback; option legs degrade empty
[ ] F9  feed: watchdog, reconnect ceiling, fresh token, subscribe-after-auth
[ ] tests: one per family, hermetic
[ ] live probe: funds / optionchain / history against a restarted server
[ ] docs: README + 01-fixable + 02-not-fixable + kb note
[ ] one commit: code + docs + kb
```
