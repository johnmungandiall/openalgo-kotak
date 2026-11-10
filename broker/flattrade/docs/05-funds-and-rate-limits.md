# 05 — Funds path and the account-wide rate limiter

**Files:** `broker/flattrade/api/funds.py`, `broker/flattrade/api/data.py`

`/api/v1/funds` is the path the dashboard's PnL display rides on, and it is
polled several times a second. It is also the path that shares Flattrade's
account-wide rate limit with order placement — which is why a slow funds read is
not merely a slow dashboard, it is a starved order path.

---

## 1. The defect

Every funds request issued **two blocking broker round-trips** —
`/PiConnectAPI/Limits` and then `/PiConnectAPI/PositionBook` — one after the
other, with no cache anywhere on the path. The same two responses were being
re-fetched 3–4 times a second.

A balance is not tick data. It cannot change that fast.

Worse, this path **bypassed the account-wide rate limiter entirely**. The busiest
caller in the process was the only one not counted against the quota (10
orders/sec, 40 data req/sec, 200/min).

---

## 2. The four fixes

All four live in `api/funds.py`, deliberately at the broker boundary rather than
at the caller — every caller gets them, including ones added later.

### 2.1 Concurrent, not serial

`api/funds.py#_fetch_funds` issues both endpoints through a
`ThreadPoolExecutor(max_workers=2)`:

```python
with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ft-funds") as pool:
    limits_future = pool.submit(fetch_data, "/PiConnectAPI/Limits", ...)
    positions_future = pool.submit(fetch_data, "/PiConnectAPI/PositionBook", ...)
    margin_data = limits_future.result()
    position_data = positions_future.result()
```

Ordering never mattered to correctness — the two responses are independent — but
issuing them together halves the wall-clock cost of a cache miss, because the
second round-trip no longer waits for the first.

### 2.2 Short-TTL cache

`_FUNDS_CACHE_TTL` defaults to **1 second** (`FLATTRADE_FUNDS_CACHE_TTL`), with
`_FUNDS_CACHE_MAXSIZE` defaulting to 8 entries. A `cachetools.TTLCache` holds the
processed result.

One second collapses a burst of polls into a single broker fetch and still reads
as live to a human. Setting `FLATTRADE_FUNDS_CACHE_TTL=0` disables the cache
entirely — `get_margin_data` then calls `_fetch_funds` directly.

**Only a usable response is cached.** An empty result stays retryable:

```python
if result:
    with _funds_cache_lock:
        _funds_cache[key] = result
```

Caching an empty result would pin "no funds" for the whole TTL and turn a single
broker hiccup into a second of blank PnL.

### 2.3 Single-flight

Concurrent misses for the same account wait on the leader's fetch instead of each
issuing its own pair of broker calls. On a rate-limited, per-account quota that
difference matters: without it, three simultaneous pollers become six broker
requests where two would do.

`api/funds.py#_Flight` is one in-progress fetch whose outcome is published to
waiters via a `threading.Event`. The leader/waiter split:

```python
with _flights_lock:
    flight = _flights.get(key)
    leader = flight is None
    if leader:
        flight = _Flight()
        _flights[key] = flight

if not leader:
    if not flight.event.wait(timeout=_FUNDS_TIMEOUT * 2):
        logger.warning("Funds single-flight wait timed out; fetching directly")
        return _fetch_funds(auth_token)
    if flight.error is not None:
        raise flight.error
    return flight.result
```

Two details that matter:

- **A wait timeout falls back to a direct fetch.** Waiting on a lock and retrying
  would fan out on failure; a bounded wait that then fetches directly does not.
- **The flight is retired in `finally`**, after `event.set()`, so the next caller
  starts a fresh one. Releasing waiters first means a waiter never sees a flight
  that has already been popped.

### 2.4 Rate-limiter participation and a per-request timeout

`api/funds.py#fetch_data` now calls `_apply_rate_limit()` before every request,
and passes `timeout=_FUNDS_TIMEOUT` (default **5 s**,
`FLATTRADE_FUNDS_TIMEOUT`).

The shared client's timeout is **120 s**, sized for multi-year history downloads.
A funds snapshot that has not answered in a few seconds is no longer useful to a
PnL display, and waiting on it keeps a request thread busy.

---

## 3. Failure handling — never raise, never be mistaken for a bad token

`api/funds.py#fetch_data` never raises and never returns a non-JSON object:

```python
except Exception as exc:  # noqa: BLE001 - a funds display must never 500
    logger.warning(f"Flattrade funds call to {endpoint} failed: {type(exc).__name__}: {exc}")
    return None
```

Two real failure modes drove this:

- **Flattrade drops pooled connections under load.** `httpx.ReadError:
  [WinError 10035]` shows up on this path and on quotes in `log/`.
- **An edge proxy can answer with an HTML error page** instead of JSON.

Either must degrade to "no funds available here" instead of escaping as a 500 —
and must **never be mistaken for an expired token**. `blueprints/auth.py` reads an
empty funds result as invalid credentials and would force the user through a
re-login. A raised exception here would surface as a forced broker re-login.

`_fetch_funds` returns `{}` on any failure, which is the documented contract for
this module. `get_margin_data` returns `{}` too.

The processing block catches `(KeyError, TypeError, ValueError)` — `TypeError`
and `ValueError` join `KeyError` because a broker response with a null or
non-numeric field would otherwise raise straight through to the Flask request
thread.

---

## 4. Credential hygiene — the cache key

`api/funds.py#_account_key`:

```python
return hashlib.sha256(str(auth_token).encode()).hexdigest()[:12]
```

Hashed, never the raw token: a cache key can end up in a debug log line and the
token is a live trading credential.

Flattrade re-issues the token on every login, so the cache necessarily starts cold
after a fresh session — which is the behaviour we want, since a new login must
never be served the previous session's numbers.

---

## 5. The rate limiter

`api/data.py` owns the account-wide limiter. It is a **dual sliding window**, not
a fixed gap.

### 5.1 The defect it replaced

The previous implementation enforced a fixed **0.55 s gap** between requests —
the most pessimistic reading of a per-minute cap (~1.8/sec, ~109/min, no
bursting). That serialised parallel history fetches: 45 symbols took
45 × 0.55 = ~25 s regardless of worker count, while the same code on Shoonya took
~2 s.

### 5.2 The replacement

Official Flattrade limits are **40 req/sec AND 200 req/min**. The limiter runs
slightly under both as margin for clock skew:

```python
FLATTRADE_MAX_PER_SECOND = 38
FLATTRADE_MAX_PER_MINUTE = 190
```

`api/data.py#_reserve_rate_limit_slot` reserves the earliest slot satisfying both
rolling windows and returns the seconds the caller must sleep. Reservation
happens inside the lock; **sleeping is the caller's job, outside the lock**, so
concurrent threads queue without serialising on the lock.

The deque holds reserved call timestamps in non-decreasing order. The earliest
permissible slot is:

- **now**, if neither window is full at that instant;
- **1 s after the Nth-most-recent reservation**, when the per-second window is
  full (that entry must age out of the rolling second first);
- **60 s after the Mth-most-recent reservation**, when the per-minute window is
  full;

whichever is latest. Entries older than 60 s can no longer constrain any future
slot and are purged.

Two entry points, because the callers differ:

- `api/data.py#_apply_rate_limit` — sync, `time.sleep`.
- `api/data.py#_apply_rate_limit_async` — `await asyncio.sleep`, so the event loop
  is not blocked.

### 5.3 Retry with backoff

`api/data.py#get_api_response` retries on a rate-limit error with exponential
backoff: `MAX_RETRIES = 3`, `RETRY_DELAY = 2.0` base, so 2 s → 4 s → 8 s.

`api/data.py#_is_rate_limit_error` is deliberately narrow — it requires
`stat == "Not_Ok"` **and** `"exceeds Limit"` (either casing) in `emsg`. A broader
match would retry genuine failures three times and triple the latency of every
error.

The same retry pattern is duplicated in the per-symbol quote fetchers
(`_fetch_single_quote_sync`, `_fetch_single_quote_async`) because they bypass
`get_api_response` to use their own client.

---

## 6. Multiquote batching

`api/data.py#BrokerData.get_multiquotes` processes symbols in batches of **10**
with a **1.1 s** delay between batches (`BATCH_SIZE`, `RATE_LIMIT_DELAY`).

This is a *separate* limit from the sliding window above, and it is now
redundant in the sense that the sliding window already enforces the real ceiling.
It is retained because it bounds the size of a single burst of threads, which the
window alone does not.

Inside a batch, `_process_quotes_batch` chooses between two execution strategies:

- **async** (`asyncio.run` + `httpx.AsyncClient`, 100 connections) when
  `USE_ASYNC` is true;
- **`ThreadPoolExecutor(max_workers=40)`** otherwise.

`USE_ASYNC` is `not _is_eventlet_patched()`, and there is a **runtime** re-check
before use:

```python
try:
    asyncio.get_running_loop()
    use_async = False
except RuntimeError:
    pass
```

`asyncio.run()` crashes if called from within an already-running event loop, and
the eventlet check alone cannot detect that.

---

## 7. Behaviour matrix — funds

| Situation | Before | After |
|---|---|---|
| 3 polls within 1 s | 3 × 2 serial broker calls | 1 × 2 concurrent broker calls |
| 6 simultaneous pollers | 6 × 2 serial broker calls | 1 × 2 concurrent broker calls |
| Transport failure (`WinError 10035`) | `{}` (but 120 s timeout) | `{}` after ≤ 5 s |
| HTML error page from a proxy | `{}` | `{}` |
| Broker answers `Not_Ok` | `{}` | `{}` |
| Broker field is `null` | **`TypeError` → 500** | `{}` |
| Successful read | 2 serial calls, no cache | 1 concurrent pair, cached 1 s |
| Rate-limit budget | **not counted** | counted |

---

## 8. Known gaps (stated, not hidden)

1. **The cache is per-process.** A gunicorn worker pool gives each worker its own
   cache, so N workers still make N × 2 broker calls per second rather than 2.
   The single-flight is likewise per-process.
2. **`_flights` is never bounded.** A pathological token churn could grow it. In
   practice a flight is popped in `finally`, so it is bounded by concurrency.
3. **The 1-second TTL is a guess at "reads as live".** It is configurable, and
   setting it to 0 disables the cache — but there is no adaptive behaviour.
4. **`get_multiquotes`' 1.1 s inter-batch delay is now the dominant cost** for a
   large universe, since the sliding window already enforces the real ceiling.
   It has not been retuned.
