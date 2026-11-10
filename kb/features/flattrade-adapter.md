# Flattrade adapter — what was modified, and where it is documented

The Flattrade broker plugin (`broker/flattrade/`) was modified so it satisfies the
Qwantonomous Engine's trading requirements. The full record lives in
`broker/flattrade/docs/` — ten documents, one per concern.

## Read this first
- `broker/flattrade/docs/README.md` — index, why the plugin needed changing, the
  Noren-behaviour table that drives almost every modification.
- `broker/flattrade/docs/01-engine-requirements.md` — the seven requirements the
  engine imposes, and the before/after gap analysis for each.

## The documents
| # | File | Covers |
|---|---|---|
| 01 | `01-engine-requirements.md` | Requirements R1–R7 + gap analysis + the service-layer contracts |
| 02 | `02-order-placement-mpp.md` | MARKET→LMT, SL-M→SL-LMT, marketable pricing, quote retry/cache, fail-loud guards, DAY/IOC, `mpp_percentage` |
| 03 | `03-squareoff-safety.md` | `close_all_positions`: read-vs-empty, per-account lock, working-SELL ledger, oversell guard, honest failures |
| 04 | `04-cancel-all-and-books.md` | `cancel_all_orders_api` guards, `normalize_order_status`, book mappers, smart-order flip clamp |
| 05 | `05-funds-and-rate-limits.md` | Funds concurrency + TTL cache + single-flight, dual sliding-window limiter |
| 06 | `06-market-data-websocket.md` | Scrip-keyed routing (#1732), client-generation guard, auth-retry path, batch subscribe, persistent session |
| 07 | `07-order-updates.md` | The order-update adapter and why Flattrade uses REST polling instead |
| 08 | `08-symbols-and-master-contract.md` | Index normalisation, tick defaults, expiry format, `&` escape |
| 09 | `09-verification-and-upgrade-risk.md` | Tests, coverage gaps, re-verify without a broker, post-upgrade checklist |
| 10 | `10-official-api-mapping.md` | Official PiConnect API → adapter, endpoint by endpoint and field by field; 12 endpoints used vs ~16 unused; 11 discrepancies |

## Official API mapping — the three findings that matter
`10-official-api-mapping.md` reads the official contract (`marketcalls/broker-api-docs`
→ `flattrade-api-docs/`) against the live code. The load-bearing conclusions:

- **The MPP layer is mandatory, not defensive.** The official `prctyp` enum is
  `LMT` / `SL-LMT` only. `MKT` and `SL-MKT` are **not documented values**, so
  `mapping/transform_data.py#transform_data` rewriting MARKET→LMT and SL-M→SL-LMT
  is the only thing making a MARKET order legal at all.
- **`_read_book`'s empty marker is the broker's own string.** The official
  PositionBook failure example is literally
  `{"stat":"Not_Ok","emsg":"Error Occurred : 5 \"no data\""}` — the exact text
  `api/order_api.py#_EMPTY_BOOK_MARKERS` keys on.
- **The scrip key is the broker's native identity.** `Exchange|Token` joined with
  `#` is what the official subscribe frame takes, so issue #1732's routing fix
  uses Flattrade's own format rather than an invented one.

### Open discrepancies (recorded, NOT fixed)
The order path is the least-guarded part of the plugin — both findings below are
the same family as the two defects that already reached production:
- `api/order_api.py#place_order_api` hard-indexes `response_data["stat"]`; a reply
  without it raises `KeyError` → generic HTTP 500. Every book reader uses `.get()`.
- `api/order_api.py` never calls `_apply_rate_limit()`, so the **order** path is
  uncounted against the official 10/sec · 40/min limit — which is 4× tighter than
  the 40/sec · 200/min limit `api/data.py` does enforce.
- `mapping/transform_data.py#reverse_map_product_type` returns `None` for `H`/`B`/`F`
  (Cover/Bracket/MTF), and `close_all_positions` passes that into the square-off
  payload's `product`. Unreachable via the app's own order path; reachable on a
  position opened in the Flattrade web terminal.
- `streaming/flattrade_websocket.py#AUTH_SUCCESS` is `"OK"` but the official ack
  writes `"Ok"`; the comparison is case-sensitive. The order adapter already
  accepts both — the market-data client does not.

## The one rule that generalises
A broker helper's reply is **not always** the documented success payload. On
failure it is the broker's error object — a bare dict, a string, `None`. Indexing
or iterating it before type-checking it raises, and the service layer reports that
`TypeError` as a failed trading operation.

Corollary: **distinguish "empty" from "unreadable" explicitly.** An empty book is
a fact; an unreadable book is an unknown. A trading decision may be made on the
first and never on the second.

## Upgrade risk
`broker/flattrade/` is vendored upstream code. Every modification is a **local
patch** — an OpenAlgo upgrade that overwrites these files silently reintroduces
the defects. The 12-point post-upgrade checklist is in
`broker/flattrade/docs/09-verification-and-upgrade-risk.md` §4.1.

See [[gotchas]] for the two `LOCAL PATCH (2026-10-22 …)` entries, and
`docs/2026-10-22-flattrade-squareoff-cancelall-fixes.md` (repo root) for the
verbatim production tracebacks.
