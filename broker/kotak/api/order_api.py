import json
import threading
import time
import urllib.parse

import httpx

from broker.kotak.api.rate_limit import apply_rate_limit
from broker.kotak.mapping.transform_data import (
    map_exchange,
    map_product_type,
    reverse_map_exchange,
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from database.token_db import get_br_symbol, get_oa_symbol, get_symbol, get_token
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


# --- Read-vs-empty distinction ------------------------------------------------
# Kotak answers a book request with {"stat": "Ok", "data": [...]} on success and
# {"stat": "Not_Ok", "emsg": ..., "stCode": ...} on failure — the failure shape
# carries NO "data" key at all (broker-api-docs/kotak-api-docs/
# 07-portfolio-positions.md §5). Collapsing "no data key" into "0" is how a
# close or a smart order used to act on a book it could not see.
#
# A genuinely empty book is stat "Ok" with an empty (or absent) data array: the
# doc says "Only positions with actual trades for the day will be listed", so an
# empty array is a normal answer. Every other non-Ok reply is a READ FAILURE.
_EMPTY_BOOK_MARKERS = ("no data",)


class BrokerReadError(Exception):
    """A Kotak book read genuinely failed (as opposed to being empty)."""


def _as_int(value):
    """Tolerant int for Kotak's string-typed numbers ("75", "75.0", "", None)."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _is_empty_book_response(response):
    """True only when a Not_Ok reply specifically means "there are no rows"."""
    if not isinstance(response, dict) or response.get("stat") != "Not_Ok":
        return False
    emsg = str(response.get("emsg") or response.get("message") or "").lower()
    return any(marker in emsg for marker in _EMPTY_BOOK_MARKERS)


def _read_book(response, what):
    """Normalise a Kotak book reply to a list of rows, or raise BrokerReadError.

    [] is a genuinely EMPTY book; raising is the only way to say "I could not
    read it", so a caller can never mistake one for the other.
    """
    if response is None:
        raise BrokerReadError(f"{what} unavailable (no response from broker)")
    if isinstance(response, list):
        return [row for row in response if isinstance(row, dict)]
    if isinstance(response, dict):
        if _is_empty_book_response(response):
            return []
        stat = response.get("stat")
        if stat == "Not_Ok":
            detail = response.get("emsg") or response.get("message") or "unknown error"
            raise BrokerReadError(f"{what} read failed ({detail})")
        if stat != "Ok":
            # Neither documented shape: no stat, or an unexpected value. We
            # cannot tell empty from unreadable, so we must not guess.
            raise BrokerReadError(
                f"{what} read failed (unexpected reply, stat={stat!r})"
            )
        data = response.get("data")
        if data is None:
            # stat said Ok and there is no data key: a genuinely empty book.
            return []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        if isinstance(data, dict):
            return [data]
        return []
    raise BrokerReadError(
        f"{what} read failed (unexpected reply type {type(response).__name__})"
    )


def _error_reply(message):
    """A documented Kotak error shape (stat "Not_Ok" + emsg).

    The underscore matters: every endpoint documents "Not_Ok"
    (06-trading-place-modify-cancel.md §5, 07-portfolio-positions.md §5,
    09-portfolio-limits.md §5, 11-order-reports.md §5.1). The previous "NotOk"
    spelling was invisible to any caller testing the documented value.
    """
    return {"stat": "Not_Ok", "emsg": str(message)}


def get_api_response(endpoint, auth_token, method="GET", payload=""):
    """
    Updated for Kotak Neo API v2 - uses dynamic baseUrl, httpx connection pooling, and new header structure
    """
    session_token, session_sid, base_url, access_token = auth_token.split(":::")

    # Debug logging for baseUrl
    logger.debug(f"ORDER API - Using baseUrl: {base_url}")

    # Get the shared httpx client with connection pooling
    client = get_httpx_client()

    headers = {
        "accept": "application/json",
        "Sid": session_sid,
        "Auth": session_token,
        "neo-fin-key": "neotradeapi",
    }

    # Construct full URL
    url = f"{base_url}{endpoint}"

    # Account-wide budget: 10 requests/second across all Kotak APIs.
    apply_rate_limit()

    # Make request using httpx
    response = client.request(method, url, headers=headers, content=payload if payload else None)

    logger.debug(f"ORDER API Response: {response.status_code} {response.text[:500]}")

    # An edge proxy can answer with an HTML error page, and a gateway timeout
    # with an empty body. Neither is JSON, and json.loads would raise past every
    # caller as a generic 500 instead of a broker-shaped error.
    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, TypeError) as error:
        logger.error(f"ORDER API non-JSON reply from {endpoint}: {error}")
        return _error_reply(f"non-JSON reply from {endpoint} (HTTP {response.status_code})")


def get_order_book(auth_token):
    return get_api_response("/quick/user/orders", auth_token)


def get_trade_book(auth_token):
    return get_api_response("/quick/user/trades", auth_token)


def get_positions(auth_token):
    return get_api_response("/quick/user/positions", auth_token)


def get_holdings(auth_token):
    # 08-portfolio-holdings.md §2: GET <Base URL>/portfolio/v1/holdings
    return get_api_response("/portfolio/v1/holdings", auth_token)


# --- Per-Symbol Smart Order Lock ---
# Ensures only one smart order per symbol executes at a time.
# Others queue and execute sequentially, each getting a fresh position book.
_symbol_locks = {}          # {symbol_key: threading.Lock}
_symbol_locks_lock = threading.Lock()

# --- Position Book Cache ---
# Caches get_positions() for 1 second. Invalidated after each smart order placement.
_position_cache = {}        # {auth_token: {"data": ..., "timestamp": ...}}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0   # seconds


def _get_symbol_lock(symbol, exchange, product):
    """Get or create a per-symbol lock for serializing smart orders."""
    key = f"{symbol}:{exchange}:{product}"
    with _symbol_locks_lock:
        if key not in _symbol_locks:
            _symbol_locks[key] = threading.Lock()
        return _symbol_locks[key]


def _get_cached_positions(auth):
    """Get positions from cache if fresh, otherwise fetch from broker API."""
    with _position_cache_lock:
        now = time.monotonic()
        cached = _position_cache.get(auth)
        if cached and (now - cached["timestamp"]) < _POSITION_CACHE_TTL:
            return cached["data"]

    # Cache miss or expired - fetch from broker
    positions_data = get_positions(auth)

    with _position_cache_lock:
        _position_cache[auth] = {"data": positions_data, "timestamp": time.monotonic()}

    return positions_data


def _invalidate_position_cache(auth):
    """Invalidate the position cache so the next queued order fetches fresh data."""
    with _position_cache_lock:
        _position_cache.pop(auth, None)


# --- Per-account square-off lock ---
# Serialises close_all_positions per auth token. Two sweeps MUST NOT read the
# same stale net quantity: each would sell the whole long and the second one
# would open a SHORT - the unintended option-selling defect this guards against.
# Unlike _symbol_locks this is account-wide, because one sweep touches every
# position at once.
_close_locks = {}            # {auth_token: threading.Lock}
_close_locks_lock = threading.Lock()

# Grace period after cancelling a stale working SELL, before re-reading the
# books. Kotak needs a moment to release the cancelled quantity.
_CLOSE_CANCEL_SETTLE = 0.15

# Kotak ordSt values that mean "still working at the exchange". Lowercase free
# text, per 11-order-reports.md §5.1 and the vocabulary already enumerated in
# streaming/kotak_order_adapter.py#_STATUS_MAP.
_WORKING_ORDER_STATUSES = frozenset(
    {
        "open",
        "trigger pending",
        "open pending",
        "modified",
        "modify pending",
        "cancel pending",
        "put order req received",
        "validation pending",
        "after market order req received",
        "modify after market order req received",
    }
)


def _get_close_lock(auth):
    """Get or create the per-account square-off lock."""
    key = auth or ""
    with _close_locks_lock:
        if key not in _close_locks:
            _close_locks[key] = threading.Lock()
        return _close_locks[key]


def _position_key(position):
    """Join key between a position row and an order-book row (both RAW Kotak)."""
    return (position.get("trdSym"), position.get("exSeg"), position.get("prod"))


def _working_sell_orders(order_rows, trade_rows):
    """WORKING SELLs, keyed by (trdSym, exSeg, prod) -> [{orderid, remaining}].

    Both books carry RAW Kotak values here (trdSym/exSeg/prod are not mapped), so
    the keys compare directly.

    The order book has no filled-quantity field (11-order-reports.md §5.1 lists
    qty, prc, avgPrc, trnsTp, prcTp, vldt — no fill quantity), so `remaining`
    must be `qty` minus the `fldQty` summed from the TRADE book for the same
    nOrdNo. Without that subtraction a partially filled SELL would be counted as
    fully working and the close would be skipped for ever.

    The ORDER IDS matter: a SELL left working by an earlier sweep was priced off
    a premium that has since moved, so it may never fill. A later sweep must
    CANCEL it and re-price rather than skip the leg - skipping leaves the
    position open for ever and a retry loop can never converge.
    """
    filled_by_order = {}
    for trade in trade_rows:
        order_id = str(trade.get("nOrdNo") or "")
        if order_id:
            filled_by_order[order_id] = filled_by_order.get(order_id, 0) + _as_int(
                trade.get("fldQty")
            )

    working = {}
    for order in order_rows:
        if str(order.get("trnsTp") or "").strip().upper() != "S":
            continue
        if str(order.get("ordSt") or "").strip().lower() not in _WORKING_ORDER_STATUSES:
            continue
        order_id = str(order.get("nOrdNo") or "")
        remaining = _as_int(order.get("qty")) - filled_by_order.get(order_id, 0)
        if remaining <= 0:
            continue
        key = (order.get("trdSym"), order.get("exSeg"), order.get("prod"))
        working.setdefault(key, []).append({"orderid": order_id, "remaining": remaining})
    return working


def _cancel_orders(order_ids, auth):
    """Best-effort cancel; returns the ids the broker really cancelled."""
    cancelled = []
    for order_id in order_ids:
        if not order_id:
            continue
        try:
            response, status_code = cancel_order(order_id, auth)
            if status_code == 200:
                cancelled.append(order_id)
            else:
                logger.warning(
                    f"close_all_positions: could not cancel stale order "
                    f"{order_id} ({response})"
                )
        except Exception as error:
            logger.error(
                f"close_all_positions: cancel of stale order {order_id} threw: {error}"
            )
    return cancelled


def get_open_position(tradingsymbol, exchange, producttype, auth_token):
    # Convert Trading Symbol from OpenAlgo Format to Broker Format Before Search in OpenPosition
    tradingsymbol = get_br_symbol(tradingsymbol, exchange)
    positions_data = _get_cached_positions(auth_token)
    logger.debug(f"{positions_data}")

    # An UNREADABLE book is NOT a flat one: BrokerReadError propagates so the
    # caller refuses to trade on a book it cannot actually see. Only a
    # genuinely EMPTY book reaches the summing loop below and yields "0".
    positions = _read_book(positions_data, "Kotak position book")

    exchange = reverse_map_exchange(exchange)

    # Sum every matching row: a contract can appear more than once (a day row
    # alongside a carry-forward row, a re-listed series), and reading only the
    # first match under-reports the position - which makes a smart order's delta
    # too large and leaves the exit half-completed.
    net_qty = 0
    for position in positions:
        if (
            position.get("trdSym") == tradingsymbol
            and position.get("exSeg") == exchange
            and position.get("prod") == producttype
        ):
            net_qty += (
                _as_int(position.get("flBuyQty")) - _as_int(position.get("flSellQty"))
            ) + (_as_int(position.get("cfBuyQty")) - _as_int(position.get("cfSellQty")))

    return str(net_qty)


def place_order_api(data, auth_token):
    session_token, session_sid, base_url, access_token = auth_token.split(":::")

    # Debug logging for baseUrl
    logger.debug(f"PLACE ORDER API - Using baseUrl: {base_url}")

    # Get the shared httpx client with connection pooling
    client = get_httpx_client()

    # Payload construction can reject the order before any network call — an
    # SL-M with no trigger, or one whose tick size is unresolvable (see
    # mapping/transform_data.py::_slm_protected_price). Return the same error
    # shape as the request failures below instead of raising past the caller.
    try:
        token_id = get_token(data["symbol"], data["exchange"])
        newdata = transform_data(data, token_id)
    except Exception as e:
        logger.error(f"Error building Kotak order payload: {e}")
        return None, _error_reply(e), None

    json_string = json.dumps(newdata)
    payload = f"jData={urllib.parse.quote(json_string)}"

    headers = {
        "accept": "application/json",
        "Sid": session_sid,
        "Auth": session_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Construct full URL
    url = f"{base_url}/quick/order/rule/ms/place"

    try:
        apply_rate_limit()
        response = client.post(url, headers=headers, content=payload)
        logger.debug(f"PLACE ORDER API Response: {response.status_code} {response.text[:500]}")

        # Add status attribute for compatibility with the existing codebase
        response.status = response.status_code

        response_data = json.loads(response.text)

        # Read the documented fields defensively: the failure shape carries no
        # nOrdNo (06-trading-place-modify-cancel.md §5), so indexing it raised
        # KeyError and surfaced as a generic 500.
        if not isinstance(response_data, dict):
            return response, _error_reply("unexpected reply from Kotak"), None
        orderid = (
            response_data.get("nOrdNo") if response_data.get("stat") == "Ok" else None
        )
        return response, response_data, orderid
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in place_order_api: {e}")
        return None, _error_reply(e), None
    except Exception as e:
        logger.error(f"Error in place_order_api: {e}")
        return None, _error_reply(e), None


def place_smartorder_api(data, auth_token):
    # If no API call is made in this function then res will return None
    res = None

    # Extract necessary info from data
    symbol = data.get("symbol")
    exchange = data.get("exchange")
    product = data.get("product")

    # Per-symbol lock: serialize smart orders per symbol
    symbol_lock = _get_symbol_lock(symbol, exchange, product)

    with symbol_lock:
        return _place_smartorder_locked_kotak(data, auth_token, symbol, exchange, product)


def _place_smartorder_locked_kotak(data, auth_token, symbol, exchange, product):
    """Inner smart order logic for kotak, called under per-symbol lock."""
    res = None
    position_size = int(data.get("position_size", "0"))

    # Read a FRESH book: a book cached before the previous fill would make this
    # order's delta too large (it still shows the pre-fill quantity).
    _invalidate_position_cache(auth_token)

    # Get current open position for the symbol. Raises BrokerReadError on a
    # genuine read failure - never silently treated as flat.
    current_position = int(
        get_open_position(symbol, exchange, map_product_type(product), auth_token)
    )

    # A target on the FAR side of zero would FLIP the position (a long sold past
    # flat becomes a short). A "reduce to size" request never means that, so
    # clamp to flat unless the caller explicitly opts in with allow_flip. This is
    # the last line of defence against an unintended option SELL, independent of
    # whatever the caller computed.
    allow_flip = str(data.get("allow_flip") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    flipping = (current_position > 0 and position_size < 0) or (
        current_position < 0 and position_size > 0
    )
    if flipping and not allow_flip:
        logger.warning(
            f"place_smartorder_api: target position {position_size} would flip "
            f"the {current_position} position on {symbol}; clamping to flat "
            f"(pass allow_flip=true to override)"
        )
        position_size = 0

    logger.debug(f"position_size : {position_size}")
    logger.debug(f"Open Position : {current_position}")

    # Determine action based on position_size and current_position
    action = None
    quantity = 0

    # If both position_size and current_position are 0, do nothing
    if position_size == 0 and current_position == 0 and int(data["quantity"]) != 0:
        action = data["action"]
        quantity = data["quantity"]
        res, response, orderid = place_order_api(data, auth_token)
        _invalidate_position_cache(auth_token)

        return res, response, orderid

    elif position_size == current_position:
        if int(data["quantity"]) == 0:
            response = {
                "status": "success",
                "message": "No OpenPosition Found. Not placing Exit order.",
            }
        else:
            response = {
                "status": "success",
                "message": "No action needed. Position size matches current position",
            }
        orderid = None
        return res, response, orderid  # res remains None as no API call was made

    if position_size == 0 and current_position > 0:
        action = "SELL"
        quantity = abs(current_position)
    elif position_size == 0 and current_position < 0:
        action = "BUY"
        quantity = abs(current_position)
    elif current_position == 0:
        action = "BUY" if position_size > 0 else "SELL"
        quantity = abs(position_size)
    else:
        if position_size > current_position:
            action = "BUY"
            quantity = position_size - current_position
        elif position_size < current_position:
            action = "SELL"
            quantity = current_position - position_size

    if action:
        # Prepare data for placing the order
        order_data = data.copy()
        order_data["action"] = action
        order_data["quantity"] = str(quantity)

        # Place the order
        res, response, orderid = place_order_api(order_data, auth_token)
        _invalidate_position_cache(auth_token)
        logger.debug(f"{response}")
        logger.debug(f"{orderid}")

        return res, response, orderid


def _resolve_position_symbol(position, exchange):
    """Resolve one Kotak position row to its OpenAlgo symbol.

    `tok` is NOT a documented positions field (07-portfolio-positions.md §5
    lists trdSym/sym, not tok), so indexing it raised KeyError and aborted the
    whole sweep mid-loop. Resolve from the trading symbol first, and use the
    token only when the broker happens to include it.
    """
    token = str(position.get("tok") or "").strip()
    if token:
        mapped = get_symbol(token, exchange)
        if mapped:
            return mapped

    broker_symbol = position.get("trdSym") or position.get("sym") or ""
    if broker_symbol:
        mapped = get_oa_symbol(broker_symbol, exchange)
        if mapped:
            return mapped

    return None


def close_all_positions(current_api_key, auth_token):
    """Square off every open position, exactly once.

    Serialised per account and DE-DUPLICATED against orders already working at
    the broker. Without both guards a second sweep reads the same stale net
    quantity, sells the same long again and takes the account through zero into
    a SHORT - the unintended option-selling defect this function now prevents.

    Returns (payload, status_code). A non-200 is a REAL failure (unreadable
    book, or a close order the broker rejected) and is propagated to the caller
    instead of being reported as a successful square-off.
    """
    with _get_close_lock(auth_token):
        # Never decide from a book cached before a previous close landed.
        _invalidate_position_cache(auth_token)

        try:
            positions = _read_book(get_positions(auth_token), "Kotak position book")
        except BrokerReadError as error:
            logger.error(f"close_all_positions: {error}")
            return {"status": "error", "message": str(error)}, 500
        except Exception as error:  # network / decode / broker 5xx
            logger.exception(f"close_all_positions: position book error: {error}")
            return {
                "status": "error",
                "message": f"Position book unavailable ({error})",
            }, 500

        # A genuinely empty book (or every row already flat) is a soft no-op.
        if not positions:
            return {"message": "No Open Positions Found"}, 200

        # A working SELL left behind by an EARLIER sweep was priced off a
        # premium that has since moved, so it may never fill. Skipping the leg
        # would leave the position open for ever and a retry loop could never
        # converge, so those orders are CANCELLED first and the close is then
        # re-priced against the current book.
        # Without a readable order book we cannot tell whether a close is already
        # working, so a sweep would risk overselling. Refuse instead.
        try:
            working_sells = _working_sell_orders(
                _read_book(get_order_book(auth_token), "Kotak order book"),
                _read_book(get_trade_book(auth_token), "Kotak trade book"),
            )
        except BrokerReadError as error:
            logger.error(f"close_all_positions: refusing to close blind: {error}")
            return {"status": "error", "message": str(error)}, 500
        except Exception as error:
            logger.exception(f"close_all_positions: order book error: {error}")
            return {
                "status": "error",
                "message": f"Order book unavailable ({error})",
            }, 500

        # Cancel any working SELL that would block (or double-count against) the
        # close we are about to place for the SAME contract.
        stale_ids = []
        for position in positions:
            net_qty = (
                _as_int(position.get("flBuyQty")) - _as_int(position.get("flSellQty"))
            ) + (_as_int(position.get("cfBuyQty")) - _as_int(position.get("cfSellQty")))
            if net_qty <= 0:
                continue
            for order in working_sells.get(_position_key(position), []):
                stale_ids.append(order["orderid"])

        cancelled_ids = _cancel_orders(stale_ids, auth_token) if stale_ids else []
        if cancelled_ids:
            logger.warning(
                f"close_all_positions: cancelled {len(cancelled_ids)} stale working "
                f"SELL(s) {cancelled_ids} so the close can be re-priced"
            )
            # Let the broker release the cancelled quantity, then decide from a
            # FRESH position book + order book - never from the pre-cancel view.
            time.sleep(_CLOSE_CANCEL_SETTLE)
            _invalidate_position_cache(auth_token)
            try:
                positions = _read_book(
                    get_positions(auth_token), "Kotak position book"
                )
                working_sells = _working_sell_orders(
                    _read_book(get_order_book(auth_token), "Kotak order book"),
                    _read_book(get_trade_book(auth_token), "Kotak trade book"),
                )
            except BrokerReadError as error:
                logger.error(
                    f"close_all_positions: re-read after cancel failed: {error}"
                )
                return {"status": "error", "message": str(error)}, 500

        closed = []
        covered = []
        failures = []

        # Loop through each position to close
        for position in positions:
            net_qty = (
                _as_int(position.get("flBuyQty")) - _as_int(position.get("flSellQty"))
            ) + (_as_int(position.get("cfBuyQty")) - _as_int(position.get("cfSellQty")))
            if net_qty == 0:
                continue

            exchange = map_exchange(position.get("exSeg"))
            if not exchange:
                logger.error(
                    f"close_all_positions: unknown exchange segment "
                    f"{position.get('exSeg')!r} for {position.get('trdSym')}"
                )
                failures.append(
                    {
                        "symbol": position.get("trdSym"),
                        "message": f"unknown exchange segment {position.get('exSeg')!r}",
                    }
                )
                continue

            symbol = _resolve_position_symbol(position, exchange)
            if not symbol:
                logger.error(
                    f"close_all_positions: no OpenAlgo symbol for "
                    f"{position.get('trdSym')} on {exchange}"
                )
                failures.append(
                    {
                        "symbol": position.get("trdSym"),
                        "message": "symbol lookup failed",
                    }
                )
                continue
            logger.debug(f"The Symbol is {symbol}")

            # Determine action based on net quantity
            action = "SELL" if net_qty > 0 else "BUY"
            quantity = abs(net_qty)

            if net_qty > 0:
                # A long: subtract whatever a working SELL the broker would NOT
                # let us cancel still covers, so this sweep can never take the
                # position through zero. pop() consumes the key so a second row
                # for the same contract cannot double-count the same order.
                key = _position_key(position)
                still_working = sum(
                    order["remaining"] for order in working_sells.pop(key, [])
                )
                if still_working:
                    quantity -= still_working
                    if quantity <= 0:
                        logger.info(
                            f"close_all_positions: {symbol} still covered by a "
                            f"working SELL for {still_working} that could not be "
                            f"cancelled - not selling again (netqty {net_qty})"
                        )
                        covered.append(
                            {"symbol": symbol, "working_sell_qty": still_working}
                        )
                        continue

            # Prepare the order payload
            place_order_payload = {
                "apikey": current_api_key,
                "strategy": "Squareoff",
                "symbol": symbol,
                "action": action,
                "exchange": exchange,
                "pricetype": "MARKET",
                "product": reverse_map_product_type(position.get("prod")),
                "quantity": str(quantity),
            }

            logger.debug(f"{place_order_payload}")

            # Place the order to close the position and HONOUR the result: a
            # rejected close must surface as a failure, not as a silent "success".
            try:
                res, response, orderid = place_order_api(place_order_payload, auth_token)
            except Exception as error:
                logger.exception(
                    f"close_all_positions: {symbol} close threw: {error}"
                )
                failures.append({"symbol": symbol, "message": str(error)})
                continue

            stat = response.get("stat") if isinstance(response, dict) else None
            http_status = getattr(res, "status", None)
            if http_status == 200 and stat == "Ok":
                closed.append(
                    {
                        "symbol": symbol,
                        "action": action,
                        "quantity": quantity,
                        "orderid": orderid,
                    }
                )
            else:
                detail = (
                    (response.get("emsg") or response.get("message"))
                    if isinstance(response, dict)
                    else None
                )
                logger.error(
                    f"close_all_positions: {symbol} close REJECTED "
                    f"(http={http_status} stat={stat} {detail})"
                )
                failures.append(
                    {
                        "symbol": symbol,
                        "message": detail or "broker rejected the close order",
                        "http_status": http_status,
                        "stat": stat,
                    }
                )

        # The book changed - never let a stale snapshot feed the next caller.
        _invalidate_position_cache(auth_token)

        if failures:
            logger.error(
                f"close_all_positions: {len(failures)} close order(s) failed: {failures}"
            )
            return {
                "status": "error",
                "message": f"{len(failures)} close order(s) failed: "
                + "; ".join(f"{f.get('symbol')}: {f.get('message')}" for f in failures),
                "closed": closed,
                "failed": failures,
            }, 500

        if closed:
            return {
                "status": "success",
                "message": "All Open Positions SquaredOff",
                "closed": closed,
            }, 200

        if covered:
            # Every open long is already covered by a working SELL: nothing was
            # sent, and nothing more may be sent without overselling.
            return {
                "status": "success",
                "message": "Close already working for every open position",
                "covered": covered,
            }, 200

        return {"message": "No Open Positions Found"}, 200


def cancel_order(orderid, auth_token):
    session_token, session_sid, base_url, access_token = auth_token.split(":::")

    # Get the shared httpx client with connection pooling
    client = get_httpx_client()

    payload = f"jData={urllib.parse.quote(json.dumps({'on': orderid, 'am': 'NO'}))}"

    headers = {
        "accept": "application/json",
        "Sid": session_sid,
        "Auth": session_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Construct full URL
    url = f"{base_url}/quick/order/cancel"

    try:
        apply_rate_limit()
        response = client.post(url, headers=headers, content=payload)
        response_data = json.loads(response.text)

        if isinstance(response_data, dict) and response_data.get("stat") == "Ok":
            return {"status": "success", "orderid": response_data.get("nOrdNo")}, 200
        detail = (
            (response_data.get("emsg") or response_data.get("message"))
            if isinstance(response_data, dict)
            else "unexpected reply from Kotak"
        )
        return {
            "status": "error",
            "message": detail or "Failed to cancel order",
        }, response.status_code
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in cancel_order: {e}")
        return {"status": "error", "message": str(e)}, 500
    except Exception as e:
        logger.error(f"Error in cancel_order: {e}")
        return {"status": "error", "message": str(e)}, 500


def modify_order(data, auth_token):
    session_token, session_sid, base_url, access_token = auth_token.split(":::")

    # Debug logging for baseUrl
    logger.debug(f"MODIFY ORDER API - Using baseUrl: {base_url}")

    # Get the shared httpx client with connection pooling
    client = get_httpx_client()

    # Same pre-flight rejection as placement (SL-M without a usable trigger or
    # tick size) — surface it as an error response, not an exception.
    try:
        token_id = get_token(data["symbol"], data["exchange"])
        newdata = transform_modify_order_data(data, token_id)
    except Exception as e:
        logger.error(f"Error building Kotak modify payload: {e}")
        return {"status": "error", "message": str(e)}, 400

    logger.debug(f"MODIFY ORDER - Transformed data: {newdata}")

    payload = f"jData={urllib.parse.quote(json.dumps(newdata))}"

    headers = {
        "accept": "application/json",
        "Sid": session_sid,
        "Auth": session_token,
        "neo-fin-key": "neotradeapi",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Construct full URL
    url = f"{base_url}/quick/order/vr/modify"

    logger.debug(f"MODIFY ORDER - Making POST request to: {url}")

    try:
        apply_rate_limit()
        response = client.post(url, headers=headers, content=payload)

        logger.debug(f"MODIFY ORDER - Response status: {response.status_code}")
        logger.debug(f"MODIFY ORDER - Response: {response.text[:500]}")

        response_data = json.loads(response.text)

        if isinstance(response_data, dict) and response_data.get("stat") == "Ok":
            return {"status": "success", "orderid": response_data.get("nOrdNo")}, 200
        detail = (
            (response_data.get("emsg") or response_data.get("message"))
            if isinstance(response_data, dict)
            else "unexpected reply from Kotak"
        )
        return {
            "status": "error",
            "message": detail or "Failed to modify order",
        }, response.status_code
    except httpx.HTTPError as e:
        logger.error(f"HTTP error in modify_order: {e}")
        return {"status": "error", "message": f"HTTP error: {str(e)}"}, 500
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in modify_order: {e}")
        return {"status": "error", "message": f"JSON decode error: {str(e)}"}, 500
    except Exception as e:
        logger.error(f"Error in modify_order: {e}")
        return {"status": "error", "message": str(e)}, 500


def cancel_all_orders_api(data, auth_token):
    # Get the order book
    order_book_response = get_order_book(auth_token)

    # A FAILED order-book read does not come back as a list: it is a bare
    # {"stat": "Not_Ok", "emsg": ...} dict. Iterating that yields its string
    # KEYS, so `order["ordSt"]` raised KeyError/TypeError and OpenAlgo's cancel
    # service turned it into HTTP 500 "Failed to cancel all orders due to
    # internal error" even though there was nothing pending to cancel.
    try:
        orders = _read_book(order_book_response, "Kotak order book")
    except BrokerReadError as error:
        logger.error(f"cancel_all_orders_api: {error}")
        return [], []

    # Filter orders still working at the exchange
    orders_to_cancel = [
        order
        for order in orders
        if str(order.get("ordSt") or "").strip().lower() in _WORKING_ORDER_STATUSES
    ]

    canceled_orders = []
    failed_cancellations = []
    logger.debug(f"{orders_to_cancel}")

    # Cancel the filtered orders
    for order in orders_to_cancel:
        orderid = order.get("nOrdNo")
        if not orderid:
            continue
        cancel_response, status_code = cancel_order(orderid, auth_token)
        if status_code == 200:
            canceled_orders.append(orderid)
        else:
            failed_cancellations.append(orderid)

    return canceled_orders, failed_cancellations
