# Broker API docs — the local mirror

`broker-api-docs/` holds vendor API documentation as markdown, so a plugin can be
read against its broker's real contract without a network round-trip.

**Source:** `marketcalls/broker-api-docs` (GitHub, `master`), the same mirror the
plugin docstrings already cite. **Unofficial conversion** — the repo's own README
says "for personal/educational reference". Treat it as a strong secondary source,
not as the vendor's signed contract.

## What is here

| Folder | Files | Grabbed |
|---|---|---|
| `broker-api-docs/kotak-api-docs/` | 16 | 2026-11-10 |
| `broker-api-docs/flattrade-api-docs/` | not mirrored locally — fetched on demand | — |

### `kotak-api-docs/` — Kotak Neo Trade API v2

| File | Covers |
|---|---|
| `README.md` | index + the unofficial-conversion caveat |
| `01-getting-started.md` | 15-minute quickstart, cURL examples |
| `02-authentication.md` | login, TOTP, `kType` Trade vs other, session `sid`/`Auth` |
| `03-static-ip-whitelisting.md` | SEBI static-IP mandate for retail algo |
| `04-market-data-instruments.md` | scrip master API |
| `05-market-data-quotes.md` | quotes / `neosymbol` |
| `06-trading-place-modify-cancel.md` | place / modify / cancel |
| `07-portfolio-positions.md` | positions |
| `08-portfolio-holdings.md` | holdings |
| `09-portfolio-limits.md` | limits (funds) |
| `10-portfolio-margins.md` | margin check |
| `11-order-reports.md` | order / trade reports |
| `12-neo-websocket.md` | market-data WebSocket |
| `13-order-position-streaming-websocket.md` | order + position streaming |
| `14-troubleshooting-faqs-error-codes.md` | error codes |
| `15-api-reference.md` | consolidated reference |

## Why it matters to the code

The Kotak plugin's docstrings cite these files by path, so the mirror is what makes
those citations resolvable:

- `broker/kotak/streaming/kotak_order_adapter.py` → `13-order-position-streaming-websocket.md`
- `broker/kotak/mapping/transform_data.py` → `03-static-ip-whitelisting.md`

Two facts from the docs that shape the plugin and are easy to miss without them:

- **Bracket (BO) and Cover (CO) orders were discontinued on Trade APIs from
  1 April 2026** (`06-trading-place-modify-cancel.md`). Any plugin code that still
  maps to BO/CO is mapping to a dead product.
- **The request body is a single URL-encoded `jData` field**, with `Sid`, `Auth`
  and `neo-fin-key: neotradeapi` headers — not a JSON body. A caller that sends
  raw JSON gets an auth-shaped failure, not a parse error.

## Refreshing

Re-run the download against the same base URL; the file names are stable:

```
https://raw.githubusercontent.com/marketcalls/broker-api-docs/master/kotak-api-docs/<file>
```

Verify after any refresh: no file should contain `<!DOCTYPE`, `<html` or
`404: Not Found` — a failed fetch writes an error page into a `.md` file and looks
like a successful download.

See [[gotchas]] for the plugin-level traps, and
`broker/flattrade/docs/10-official-api-mapping.md` for the worked example of
reading a vendor contract against an adapter field by field.
