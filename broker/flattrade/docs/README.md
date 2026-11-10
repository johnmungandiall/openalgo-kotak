# Flattrade adapter — modification record

**What this folder is:** the complete record of every change made to the
Flattrade broker plugin (`broker/flattrade/`) so it satisfies the Qwantonomous
Engine's trading requirements.

**Written:** 2026-11-10, against the tree shipped as
`qwantonomous-flattrade-fix-1.0.3`.

**Ground rule for every document here:** each claim is read from the live tree,
not from memory or from an earlier report. Code is cited as `path#symbol`
(never `path:line` — a line number rots on the next edit, a symbol name does
not).

---

## Why the plugin needed changing

Flattrade runs the **Noren OMS** — the same order-management system as Shoonya,
Zebu and Tradesmart. That single fact drives almost every modification in this
folder, because Noren behaves differently from a plain REST broker in ways that
break a strategy engine:

| Noren behaviour | What it breaks in the engine | Where it is handled |
|---|---|---|
| Rejects a raw `MKT` / `SL-MKT` for API orders | Every MARKET entry and every square-off is dead on arrival | `mapping/transform_data.py#transform_data` |
| A book read can answer with a bare `{"stat":"Not_Ok"}` **dict** instead of a list | Indexing/iterating it raises, the service layer turns that into HTTP 500 | `api/order_api.py#_read_book` |
| One WebSocket connection per user id | A dedicated order-update socket evicts the market-data socket | `services/order_update_service.py#_POLLING_BROKERS` |
| Tokens roll over daily at 5–6 AM IST | A long-lived socket reconnects with a dead token | `streaming/flattrade_adapter.py#_attempt_reconnection` |
| Account-wide rate limits (40/s, 200/min) | The busiest caller (funds) was the only one not counted | `api/data.py#_apply_rate_limit` |
| Noren tokens are unique only **within** an exchange | Two instruments merge into one cache slot and publish each other's price | `streaming/flattrade_adapter.py#MarketDataCache` |

The engine-side contracts these changes serve are documented in
`01-engine-requirements.md`.

---

## The documents

| # | File | Covers |
|---|---|---|
| 01 | `01-engine-requirements.md` | What the engine needs from a broker adapter, and the gap analysis that produced this work |
| 02 | `02-order-placement-mpp.md` | MARKET→LMT and SL-M→SL-LMT conversion, marketable-limit pricing, quote retry/cache, fail-loud guards, DAY/IOC retention, `mpp_percentage` |
| 03 | `03-squareoff-safety.md` | `close_all_positions`: read-vs-empty distinction, per-account lock, stale working-SELL cancel + re-price, the oversell guard, honest failures |
| 04 | `04-cancel-all-and-books.md` | `cancel_all_orders_api` response-shape guards, `normalize_order_status`, order/position book mapping, `place_smartorder_api` flip clamp |
| 05 | `05-funds-and-rate-limits.md` | Funds concurrency + TTL cache + single-flight, the dual sliding-window rate limiter, retry-with-backoff |
| 06 | `06-market-data-websocket.md` | Scrip-keyed routing (issue #1732), the client-generation guard, the auth-failure retry path, batch subscribe, the persistent-session model |
| 07 | `07-order-updates.md` | The order-update adapter, its Noren handshake, and why Flattrade is served by REST polling instead |
| 08 | `08-symbols-and-master-contract.md` | Index symbol normalisation, tick-size defaults, expiry format, BSE index detection, supported exchanges |
| 09 | `09-verification-and-upgrade-risk.md` | The tests that pin this behaviour, how to re-verify without a broker, and the upgrade checklist |
| 10 | `10-official-api-mapping.md` | **Endpoint-by-endpoint, field-by-field mapping of the official Flattrade PiConnect API onto the adapter** — every request/response field, the 12 endpoints used vs the ~16 unused, and 11 discrepancies found while mapping |

---

## Reading order

- **New to the plugin?** 01 → 02 → 03. That is the order path, which is where
  the money is.
- **Chasing a live defect?** 03 (square-off) and 04 (cancel-all) first — those
  are the two that produced production HTTP 500s.
- **Asking "what does the adapter actually send to Flattrade?"** 10. It is the
  only document that reads the official API contract alongside the code, field by
  field.
- **Chasing a slow dashboard or a frozen chart?** 05 (funds) and 06 (feeds).
- **About to upgrade OpenAlgo?** 09, before anything else.

---

## Related documents outside this folder

| Document | Relationship |
|---|---|
| `docs/2026-10-22-flattrade-squareoff-cancelall-fixes.md` (repo root) | The dated incident write-up for the two response-shape defects. Contains the verbatim production tracebacks. This folder supersedes it as the *reference*, and keeps it as the *history*. |
| `broker/flattrade/streaming/docs/reconnection_fix.md` | The original note on the persistent-session model. Document 06 extends it with everything added since. |
| `kb/gotchas.md` (repo root) | The two `LOCAL PATCH (2026-10-22 …)` entries that must survive any upgrade. |
| `utils/mpp_slab.py` | The shared MPP slab every broker uses. Flattrade is the heaviest consumer. |

---

## What is NOT in this folder

- **No behaviour was invented.** Every modification described here exists in the
  tree. Where something is a known gap, it is labelled as a gap, not as a
  feature.
- **No engine-side (Dart/Flutter) code.** The Qwantonomous Engine client lives in
  a different repository. This folder documents the *server-side adapter* the
  engine talks to.
- **No release notes.** Nothing here was released as part of this work.
