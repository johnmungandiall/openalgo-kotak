# Kotak Neo adapter — defect register

What this folder is: a code-level register of every place where the Kotak Neo
adapter (`broker/kotak/`) does not meet the engine requirements that the
Flattrade adapter already meets, split into what **can** be fixed from the
official API documentation and what **cannot**.

**Status: the 18 fixable defects are implemented and verified.** The 4
not-fixable items remain open by nature — they need a live Kotak session or a
different data source, not a code change. See
[`01-fixable-defects.md`](01-fixable-defects.md) for the file-by-file record
and the verification commands.

## Why this exists

The Flattrade adapter was brought up to the engine's seven requirements
(R1–R7, see `broker/flattrade/docs/01-engine-requirements.md`). The question
was whether Kotak meets the same bar. It does not, and the gap is not uniform:
some defects are ordinary bugs with an obvious fix, and a few are blocked by
something the official API does not provide at all. Mixing the two into one
list is how a "we'll fix it later" item quietly becomes permanent, so they are
separated here.

## The two documents

| Document | Covers | Count | Status |
| --- | --- | --- | --- |
| [`01-fixable-defects.md`](01-fixable-defects.md) | Defects with a fix that the official docs fully specify | 18 | **Implemented** |
| [`02-not-fixable.md`](02-not-fixable.md) | Items the official API cannot support, or that contradict it | 4 | N1 **resolved** (Yahoo fallback); N2–N4 open |

## Evidence standard

Every claim in both documents is grounded in one of two things, and each
defect names which:

- **Code** — the real implementation, cited as `path#symbol` (never a line
  number, which rots on the next edit).
- **Official doc** — the mirrored Kotak Neo Trade API v2 documentation in
  `broker-api-docs/kotak-api-docs/` (16 files, see that folder's `README.md`).

Where the two disagree, the disagreement is stated rather than resolved by
preference. One such case (SL-M) is in `02-not-fixable.md` precisely because
the doc and live behaviour contradict each other and the doc cannot settle it.

## Requirement mapping

The seven engine requirements, and where Kotak stands on each. Full detail is
in the two documents; this is the summary.

| Req | What it demands | Before | After | Defects |
| --- | --- | --- | --- | --- |
| R1 | Order placement, MPP, DAY/IOC | Partial | **Pass** | K7, K8, K9, K10 |
| R2 | Square-off cannot oversell | Fail | **Pass** | K1, K2, K3, K14 |
| R3 | Unreadable book ≠ flat book | Fail | **Pass** | K4, K13, K15 |
| R4 | Honest failures, never false success | Fail | **Pass** | K1, K11, K17, K18 |
| R5 | Market-data feed resilience | Pass | Pass | — |
| R6 | Funds correctness + load control | Partial | **Pass** | K5, K6, K16 |
| R7 | No credentials in logs | Fail | **Pass** | K12 |

R1 is now complete for order placement **and** for index/equity history, which
arrives via the Yahoo Finance fallback ported from PR
[`marketcalls/openalgo#1466`](https://github.com/marketcalls/openalgo/pull/1466).
Option-leg history remains unavailable (Yahoo carries no Indian F&O), so the
OI/IV/straddle charts that need option legs still degrade to empty — see
[`02-not-fixable.md`](02-not-fixable.md) N1.

R5 is the one area where Kotak is **better** than Flattrade: the order-update
adapter (`broker/kotak/streaming/kotak_order_adapter.py`) has a connect-ack
watchdog and handshake-encoding alternation, and the market-data adapter
(`broker/kotak/streaming/kotak_adapter.py`) has exponential-backoff
reconnection, debounced batch subscription and partial-update merging.
Flattrade polls REST for order updates instead. Nothing in R5 needs fixing.

## Reading order

1. `01-fixable-defects.md` — start with K1–K4. Those four are the ones that can
   move real money: a square-off that reports success without checking the
   broker's reply, a second sweep that can open a short, and a position book
   that reads as flat when it could not be read at all.
2. `02-not-fixable.md` — read before promising a user that Kotak will have
   historical data or open interest. It will not, from this API.

## Related

- `broker/flattrade/docs/01-engine-requirements.md` — the R1–R7 definitions and
  the reference implementation of each.
- `broker/flattrade/docs/03-squareoff-safety.md` — the square-off guards that
  K1–K3 are missing.
- `broker/flattrade/docs/10-official-api-mapping.md` — the same
  official-doc-to-code mapping exercise, done for Flattrade.
- `broker-api-docs/kotak-api-docs/README.md` — the mirrored official docs.
