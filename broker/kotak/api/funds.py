# api/funds.py
"""Kotak funds / margin.

`/api/v1/funds` is polled by the dashboard several times a second, and every
poll used to be a broker round-trip against Kotak's account-wide budget of
10 requests/second (`broker-api-docs/kotak-api-docs/
14-troubleshooting-faqs-error-codes.md` Q24). A balance cannot change that
fast, so most of that quota bought nothing — and it is quota the order path
needs.

Three things are fixed here rather than at the caller:

1. The processed result is served from a short-TTL, process-wide cache, and
   concurrent misses collapse into a **single** broker fetch (single-flight).
2. `availablecash` is computed from `Net`, the field the official Limits
   response documents as "Net available margin/cash"
   (`09-portfolio-limits.md` §5). It previously summed `RmsPayInAmt`,
   `RmsPayOutAmt` and `Collateral`, **none of which appear in that response** —
   so `.get(..., 0)` made them silently zero and `availablecash` degenerated to
   `CollateralValue` alone. No error, no warning: a wrong number presented as
   the account's available funds.
3. The full broker response is no longer written to the debug log. It carries
   the account's live limits, and a debug log is a file that gets shipped in
   support bundles.

Only a usable response is cached: an empty result stays retryable, so one
broker hiccup cannot pin "no funds" for the whole TTL. A transport failure or a
non-JSON body still returns ``{}`` — the contract callers already depend on
(`blueprints/auth.py` reads an empty result as an invalid token, so a raised
exception here would surface as a forced broker re-login).
"""

import hashlib
import json
import os
import threading

import httpx
from cachetools import TTLCache

from broker.kotak.api.rate_limit import apply_rate_limit
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# A funds snapshot is not tick data: the dashboard polls faster than a balance
# can change. One second collapses a burst of polls into a single broker fetch
# and still reads as live to a human. Set KOTAK_FUNDS_CACHE_TTL=0 to disable.
_FUNDS_CACHE_TTL = float(os.getenv("KOTAK_FUNDS_CACHE_TTL", "1"))
_FUNDS_CACHE_MAXSIZE = int(os.getenv("KOTAK_FUNDS_CACHE_MAXSIZE", "8"))

# The shared client's timeout is sized for large historical downloads. A funds
# snapshot that has not answered in a few seconds is no longer useful to a PnL
# display, and waiting on it keeps a request thread busy.
_FUNDS_TIMEOUT = float(os.getenv("KOTAK_FUNDS_TIMEOUT", "5"))

_funds_cache: TTLCache = TTLCache(
    maxsize=max(_FUNDS_CACHE_MAXSIZE, 1), ttl=max(_FUNDS_CACHE_TTL, 0.001)
)
_funds_cache_lock = threading.Lock()


class _Flight:
    """One in-progress funds fetch, whose outcome is published to waiters."""

    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: BaseException | None = None


# Concurrent misses for the same account wait on the leader's fetch instead of
# each issuing its own broker call. On a rate-limited, per-account quota that
# difference matters: without it, three simultaneous pollers become three broker
# requests where one would do.
_flights: dict[str, _Flight] = {}
_flights_lock = threading.Lock()


def _account_key(auth_token) -> str:
    """Cache key for the account behind this token.

    Hashed, never the raw token: a cache key can end up in a debug log line and
    the token is a live trading credential. Kotak re-issues the token on every
    login, so the cache necessarily starts cold after a fresh session — which is
    the behaviour we want, since a new login must never be served the previous
    session's numbers.
    """
    return hashlib.sha256(str(auth_token).encode()).hexdigest()[:12]


def _parse_auth(auth_token):
    """Split the composite auth token, or return None when it is malformed.

    Format: trading_token:::trading_sid:::base_url:::access_token
    """
    parts = str(auth_token or "").split(":::")
    if len(parts) != 4:
        logger.error(
            f"Invalid auth token format. Expected 4 parts, got {len(parts)}"
        )
        return None
    trading_token, trading_sid, base_url, access_token = parts
    if not base_url:
        logger.error("Base URL not found in auth token")
        return None
    return trading_token, trading_sid, base_url, access_token


def _fetch_funds(auth_token):
    """Issue the limits request and process it into funds data.

    Returns ``{}`` on any failure, which is the documented contract for this
    module (`get_margin_data` returns an empty dict rather than raising).
    """
    parsed = _parse_auth(auth_token)
    if parsed is None:
        return {}
    trading_token, trading_sid, base_url, _access_token = parsed

    logger.debug(f"Fetching margin data from {base_url}")

    client = get_httpx_client()

    # Prepare payload as per Kotak API docs: jData with seg, exch, prod
    payload = (
        "jData=%7B%22seg%22%3A%22ALL%22%2C%22exch%22%3A%22ALL%22%2C%22prod%22%3A%22ALL%22%7D"
    )

    headers = {
        "accept": "application/json",
        "Sid": trading_sid,
        "Auth": trading_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    url = f"{base_url}/quick/user/limits"

    try:
        # Account-wide budget: 10 requests/second across all Kotak APIs.
        apply_rate_limit()
        response = client.post(url, headers=headers, content=payload, timeout=_FUNDS_TIMEOUT)
    except httpx.HTTPError as e:
        logger.error(f"HTTP request failed while fetching margin data: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error fetching margin data: {e}")
        return {}

    # Log the shape, never the body: the response carries the account's live
    # limits and this is a debug log that gets shipped.
    logger.debug(f"Kotak Limits API Response Status: {response.status_code}")

    try:
        margin_data = json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"Failed to parse margin data JSON: {e}")
        return {}

    if not isinstance(margin_data, dict):
        logger.error("Kotak Limits API returned an unexpected reply type")
        return {}

    # Check for API errors
    if margin_data.get("stat") != "Ok":
        error_msg = margin_data.get("emsg") or margin_data.get("message") or "Unknown error"
        logger.error(f"Kotak Limits API error: {error_msg}")
        return {}

    try:
        # `Net` is the documented "Net available margin/cash"
        # (09-portfolio-limits.md §5). The previous formula summed
        # RmsPayInAmt / RmsPayOutAmt / Collateral, none of which the doc lists,
        # so all three silently evaluated to zero.
        net_available = float(margin_data.get("Net", 0))
        collateral_value = float(margin_data.get("CollateralValue", 0))
        margin_used = float(margin_data.get("MarginUsed", 0))

        processed_margin_data = {
            "availablecash": f"{net_available:.2f}",
            "collateral": f"{collateral_value:.2f}",
            "m2munrealized": f"{float(margin_data.get('UnrealizedMtomPrsnt', 0)):.2f}",
            "m2mrealized": f"{float(margin_data.get('RealizedMtomPrsnt', 0)):.2f}",
            "utiliseddebits": f"{margin_used:.2f}",
        }

        logger.info(f"Successfully fetched margin data: {processed_margin_data}")
        return processed_margin_data

    except (KeyError, TypeError, ValueError) as e:
        # A broker response with a null or non-numeric field would otherwise
        # raise straight through to the Flask request thread.
        logger.error(f"Error processing margin data: {e}")
        return {}


def get_margin_data(auth_token):
    """Fetch and process margin data.

    Served from a short-TTL cache when one is warm; callers that miss together
    share a single broker fetch. Returns ``{}`` when the broker cannot be
    reached or answers with an error, exactly as before.

    The TTL bounds how long a *successful* answer may be replayed. It is
    deliberately short (1s default) so that a token revoked moments ago still
    fails validation on essentially the next call, and so that a transport
    failure is never masked by a stale success.
    """
    if _FUNDS_CACHE_TTL <= 0:
        return _fetch_funds(auth_token)

    key = _account_key(auth_token)

    with _funds_cache_lock:
        cached = _funds_cache.get(key)
    if cached is not None:
        logger.debug("Kotak funds cache hit")
        return cached

    with _flights_lock:
        flight = _flights.get(key)
        leader = flight is None
        if leader:
            flight = _Flight()
            _flights[key] = flight

    if not leader:
        # Share the leader's outcome rather than adding a second identical call
        # to a rate-limited account. Waiting on a lock and retrying would fan
        # out on failure instead.
        if not flight.event.wait(timeout=_FUNDS_TIMEOUT * 2):
            logger.warning("Funds single-flight wait timed out; fetching directly")
            return _fetch_funds(auth_token)
        if flight.error is not None:
            raise flight.error
        return flight.result

    result: dict = {}
    try:
        result = _fetch_funds(auth_token)
        # Only a usable response is cached. An empty result is a failure —
        # caching it would pin "no funds" for the whole TTL and turn a single
        # broker hiccup into a second of blank PnL.
        if result:
            with _funds_cache_lock:
                _funds_cache[key] = result
        return result
    except BaseException as exc:  # noqa: BLE001 - propagated to waiters verbatim
        flight.error = exc
        raise
    finally:
        # Release waiters first, then retire the flight so the next caller
        # starts a fresh one.
        flight.result = result
        flight.event.set()
        with _flights_lock:
            _flights.pop(key, None)


def clear_funds_cache() -> None:
    """Drop the cached funds snapshot (test / administrative helper)."""
    with _funds_cache_lock:
        _funds_cache.clear()
