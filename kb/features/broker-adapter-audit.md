# Broker ↔ engine audit — how a plugin is brought up to R1–R7

A plugin can "work" on the happy path and still be unusable by the engine. This
note indexes the audit procedure and the record for each broker audited so far.

**Procedure:** `.claude/skills/broker-engine-audit/SKILL.md`
**Requirement definitions:** `broker/flattrade/docs/01-engine-requirements.md` (R1–R7)

## Status per broker

| Broker | Audit record | Defects | Status |
|---|---|---|---|
| Flattrade | `broker/flattrade/docs/` (11 files) | — (reference) | audited + fixed |
| Kotak | `broker/kotak/docs/` (3 files) | K1–K19 | audited + fixed |
| all others (~33) | — | — | **not audited** |

## The seven requirements, one line each

- **R1** MARKET order must reach the exchange
- **R2** square-off must never take the account through zero
- **R3** "nothing to do" must not look like a failure
- **R4** a failure must not look like success ← most dangerous
- **R5** a feed must survive a drop without lying about it
- **R6** a balance read must not cost two round-trips per poll
- **R7** a credential must never appear in a log or a cache key

## Defect families to check in every broker

`F1` reply shape (error object vs documented success payload) · `F2` square-off
safety (read-vs-empty, lock, working-SELL ledger, oversell guard, honest report) ·
`F3` funds (documented field names, `collateral` ≠ balance, PnL source, cache) ·
`F4` order mapping (pricetype, MARKET/SL-M emulation, product, `DAY`/`IOC`) ·
`F5` credentials in logs · `F6` rate limiter implemented **and** called ·
`F7` options chain (canonical leg keys, `tick_size`, per-request symbol cap,
N-in → N-out) · `F8` history (endpoint or documented fallback) · `F9` feed
lifecycle (watchdog, reconnect ceiling, fresh token, subscribe-after-auth)

## Evidence standard

Every defect needs **both** a code cite (`path#symbol`) and an official-doc cite
(`broker-api-docs/<broker>-api-docs/NN-*.md`). A defect with only one is a
hypothesis, and must be labelled as such.

- `broker-api-docs/` is an **unofficial conversion** of vendor docs — see
  [[broker-api-docs]].
- `open_int` on Kotak was a *guess* that a live call happened to confirm; the
  doc never listed it. Guess vs documented vs live-verified are three different
  evidence levels — say which.

## Why this exists

Both audits found the same root cause wearing different names: a broker helper's
reply was used as if it were always the documented success payload. On failure it
is the error object (bare dict, string, `None`, HTML), so indexing or iterating
raises — or worse, silently yields zero and the engine reads that as real data.
