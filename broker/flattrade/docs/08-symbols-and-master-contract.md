# 08 — Symbols, master contract, and exchange support

**Files:** `broker/flattrade/database/master_contract_db.py`,
`broker/flattrade/plugin.json`, `broker/flattrade/streaming/flattrade_mapping.py`

The engine addresses instruments by **OpenAlgo symbol** (`NIFTY24OCT22800CE`,
`NIFTY`, `SENSEX`). Flattrade addresses them by **token** plus a broker symbol.
This document covers the translation layer and the traps in it.

---

## 1. Supported exchanges

`plugin.json`:

```json
"supported_exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS", "MCX", "NSE_INDEX", "BSE_INDEX"],
"broker_type": "IN_stock",
"leverage_config": false
```

`leverage_config: false` matters: the engine must not offer a leverage control for
this broker, because Flattrade does not expose one.

`flattrade_mapping.py#FlattradeExchangeMapper` maps the two index pseudo-exchanges
onto the base exchange, because indices stream on `NSE` / `BSE`:

| OpenAlgo | Flattrade |
|---|---|
| `NSE`, `BSE`, `NFO`, `BFO`, `MCX`, `CDS` | same |
| `NSE_INDEX` | `NSE` |
| `BSE_INDEX` | `BSE` |

The reverse map is derived (`FLATTRADE_TO_OPENALGO`), which means it is **not**
injective: `NSE` maps back to whichever key was written last. Callers that need
the OpenAlgo exchange use the subscription's stored `exchange`, not the reverse
map — which is why `_create_subscription` keeps both.

---

## 2. The master contract download

`master_contract_db.py#master_contract_download` downloads eight CSVs from
Flattrade's S3 bucket, processes each into the `symtoken` table, then deletes the
temp files.

| Key | Source file |
|---|---|
| `NSE` | `NSE_Equity.csv` |
| `NFO_EQ` | `Nfo_Equity_Derivatives.csv` |
| `NFO_IDX` | `Nfo_Index_Derivatives.csv` |
| `CDS` | `Currency_Derivatives.csv` |
| `MCX` | `Commodity.csv` |
| `BSE` | `BSE_Equity.csv` |
| `BFO_IDX` | `Bfo_Index_Derivatives.csv` |
| `BFO_EQ` | `Bfo_Equity_Derivatives.csv` |

`combine_nfo_files` and `combine_bfo_files` concatenate the equity and index
derivative files into `NFO.csv` / `BFO.csv` before processing.

The `symtoken` table (`master_contract_db.py#SymToken`) carries
`symbol, brsymbol, name, exchange, brexchange, token, expiry, strike, lotsize,
instrumenttype, tick_size`, with a composite index on `(symbol, exchange)`.

`copy_from_dataframe` filters out tokens that already exist before bulk-inserting,
so a re-download is incremental rather than duplicative.

---

## 3. Index symbol normalisation — the trap

Flattrade's index names do not match OpenAlgo's. The engine hardcodes `NIFTY`,
`BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY`, `SENSEX`, so a mismatch means the engine
cannot find the index at all.

### 3.1 NSE_INDEX

`master_contract_db.py#process_flattrade_nse_data` uppercases and strips spaces
and hyphens, then applies explicit overrides:

| Flattrade | OpenAlgo |
|---|---|
| `NIFTY50` | `NIFTY` |
| `NIFTYBANK` | `BANKNIFTY` |
| `NIFTYFIN`, `NIFTYFINSERVICE`, `NIFTYFINANCIALSERVICES` | `FINNIFTY` |
| `NIFTYNEXT50` | `NIFTYNXT50` |
| `NIFTYMIDSELECT`, `NIFTYMIDCAPSELECT` | `MIDCPNIFTY` |

Three different Flattrade spellings collapse onto `FINNIFTY`, and two onto
`MIDCPNIFTY`. That is not redundancy — Flattrade has renamed these indices over
time and the master contract carries the old names for a while.

### 3.2 BSE_INDEX

`master_contract_db.py#process_flattrade_bse_data` detects indices by
`instrumenttype == "UNDIND"` (not `"INDEX"`, which is what NSE uses), then
normalises:

| Flattrade | OpenAlgo |
|---|---|
| `BSESENSEX`, `S&PBSESENSEX` | `SENSEX` |
| `BSESENSEX50`, `S&PBSESENSEX50` | `SENSEX50` |
| `BSESENSEXNEXT50`, `S&PBSESENSEXNEXT50` | `BSESENSEXNEXT50` |

The `&` in `S&P…` is why the overrides exist: the generic normaliser strips spaces
and hyphens but not `&`.

---

## 4. Tick size

Tick size is written into the master contract with a **per-exchange default**:

| Exchange | Default tick |
|---|---|
| NSE | 0.05 |
| NFO | 0.05 |
| BSE | 0.05 |
| BFO | 0.05 |
| MCX | 0.05 |
| **CDS** | **0.0025** |

These are defaults, not the true per-instrument tick. The real tick comes from the
broker's quote (`ti` field) at order time, and
`mapping/transform_data.py#_tick_size_from_master` is only the fallback when the
quote omits it.

The CDS value is the one that matters: currency derivatives tick at 0.0025, and
using 0.05 there would round a limit price off-tick and get it rejected.

---

## 5. Expiry format

Flattrade's CSVs carry expiry as `DD-MMM-YYYY` (e.g. `28-Aug-2025`). The master
contract stores it as **`DD-MMM-YY`** (e.g. `28-AUG-25`), and the OpenAlgo symbol
embeds it as **`DDMMMYY`** (e.g. `28AUG25`).

The pipeline:

1. `format_expiry_date` — `%d-%b-%Y` → `%d%b%y`, uppercased.
2. `format_symbol` — builds `{name}{expiry}{strike}{type}` or `{name}{expiry}FUT`.
3. `add_hyphens_to_expiry` — `28AUG25` → `28-AUG-25` for storage.

An invalid date returns `None` from step 1 and is logged, not raised — one bad row
must not abort the whole download.

---

## 6. Symbol construction

`format_symbol` (repeated in the NFO, CDS, MCX and BFO processors):

- **Futures** (`optiontype == "XX"` → `instrumenttype = "FUT"`):
  `{name}{expiry}FUT`
- **Options**: `{name}{expiry}{strike}{CE|PE}`, where the strike is rendered as an
  integer when it is whole (`25000`, not `25000.0`) and as a float otherwise.

The integer rendering is not cosmetic: `NIFTY24OCT25000CE` and
`NIFTY24OCT25000.0CE` are different strings, and only the first matches what the
engine sends.

---

## 7. The `&` escape

`mapping/transform_data.py#transform_data` and
`transform_modify_order_data` both escape `&` in the broker symbol:

```python
if symbol and "&" in symbol:
    symbol = symbol.replace("&", "%26")
```

Noren's `tsym` is form-encoded inside `jData`, so a raw `&` would terminate the
field. This affects `M&M`, `M&MFIN`, and the `S&P` index names.

---

## 8. The `NSE_INDEX` / `BSE_INDEX` exchange rewrite at quote time

`api/data.py#BrokerData.get_quotes`, `get_depth` and `get_history` all rewrite the
exchange before calling the broker:

```python
if exchange == "NSE_INDEX":
    exchange = "NSE"
elif exchange == "BSE_INDEX":
    exchange = "BSE"
```

The same rewrite appears in `_process_quotes_batch` as `api_exchange`, kept
separate from the caller-facing `exchange` so the result rows still report
`NSE_INDEX` to the engine.

This is the same fact as §1, applied at a different layer. Both are needed: the
mapper handles the WebSocket scrip, the data layer handles the REST quote.

---

## 9. Known gaps (stated, not hidden)

1. **Tick sizes in the master contract are per-exchange defaults, not real.** A
   CDS instrument that ticks at something other than 0.0025, or an NFO instrument
   that ticks at 0.01, is wrong in the table. The quote's `ti` field corrects it
   at order time; anything reading `symtoken.tick_size` directly gets the default.
2. **`delete_symtoken_table()` deletes the WHOLE table**, not just Flattrade's
   rows. That is correct for a single-broker install and wrong for a
   multi-broker one — but it is how every plugin in this tree behaves, so changing
   it is a cross-broker decision, not a Flattrade one.
3. **The reverse exchange map is not injective** (§1). Nothing currently depends
   on it being so, but a future caller could be surprised.
4. **`process_flattrade_nse_data` raises on a missing required column**, while the
   other processors do not validate columns at all. A schema change at Flattrade
   would fail loudly for NSE and silently produce wrong rows for the rest.
5. **`download_csv_data` uses a 10-second timeout and no retry.** A slow S3
   response drops that exchange's file, and `master_contract_download` then fails
   on the missing file rather than retrying.
