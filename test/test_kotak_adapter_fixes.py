"""Behavioural tests for the Kotak adapter fixes (K1-K18).

These exercise the real functions with the broker boundary monkeypatched, so a
regression in the guard logic fails here rather than in production. The
money-critical ones are the square-off guards (K1-K4) and the flip clamp (K14):
each of those, if it regresses, can open an unintended short position.

Run: .venv/Scripts/python.exe -m pytest test/test_kotak_adapter_fixes.py -q
"""

import pytest

from broker.kotak.api import order_api
from broker.kotak.mapping import transform_data as td

AUTH = "trade-token:::trade-sid:::https://example.invalid:::access-token"


# ---------------------------------------------------------------------------
# K4 - read-vs-empty distinction
# ---------------------------------------------------------------------------


def test_read_book_empty_ok_reply_is_empty_list():
    """stat Ok with no rows is a genuinely EMPTY book, not a failure."""
    assert order_api._read_book({"stat": "Ok", "data": []}, "book") == []
    assert order_api._read_book({"stat": "Ok", "data": None}, "book") == []
    assert order_api._read_book({"stat": "Ok"}, "book") == []


def test_read_book_error_reply_raises():
    """The documented error shape carries no data key - it must NOT read as 0."""
    with pytest.raises(order_api.BrokerReadError):
        order_api._read_book(
            {"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003}, "book"
        )


def test_read_book_no_data_marker_is_empty():
    """A Not_Ok that specifically says 'no data' is an empty book."""
    assert order_api._read_book({"stat": "Not_Ok", "emsg": "no data"}, "book") == []


def test_read_book_lowercase_stat_ok_is_readable():
    """Live Kotak replies carry lowercase stat='ok'; must not raise (regression)."""
    reply = {"stat": "ok", "data": [{"exSeg": "NSE", "flBuyQty": "10"}]}
    assert order_api._read_book(reply, "Kotak position book") == reply["data"]


def test_read_book_lowercase_not_ok_data_marker_is_empty():
    """Lowercase 'not_ok' with the no-data marker must read as empty too."""
    assert order_api._read_book({"stat": "not_ok", "emsg": "no data"}, "book") == []


def test_read_book_none_and_garbage_raise():
    with pytest.raises(order_api.BrokerReadError):
        order_api._read_book(None, "book")
    with pytest.raises(order_api.BrokerReadError):
        order_api._read_book("a string", "book")
    with pytest.raises(order_api.BrokerReadError):
        order_api._read_book({"unexpected": True}, "book")


def test_get_open_position_raises_on_unreadable_book(monkeypatch):
    """K4: an unreadable book must never be reported as a flat position."""
    monkeypatch.setattr(order_api, "get_br_symbol", lambda s, e: s)
    monkeypatch.setattr(
        order_api,
        "_get_cached_positions",
        lambda auth: {"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003},
    )
    with pytest.raises(order_api.BrokerReadError):
        order_api.get_open_position("NIFTY", "NFO", "NRML", AUTH)


def test_get_open_position_sums_every_matching_row(monkeypatch):
    """K13: a contract can appear more than once; reading only the first
    match under-reports the position and makes the exit half-complete."""
    monkeypatch.setattr(order_api, "get_br_symbol", lambda s, e: s)
    monkeypatch.setattr(order_api, "reverse_map_exchange", lambda e: "nse_fo")
    monkeypatch.setattr(
        order_api,
        "_get_cached_positions",
        lambda auth: {
            "stat": "Ok",
            "data": [
                {"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
                 "flBuyQty": "75", "flSellQty": "0"},
                {"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
                 "flBuyQty": "75", "flSellQty": "0"},
            ],
        },
    )
    assert order_api.get_open_position("NIFTY", "NFO", "NRML", AUTH) == "150"


# ---------------------------------------------------------------------------
# K2 - working-SELL ledger
# ---------------------------------------------------------------------------


def test_working_sell_orders_subtracts_filled_from_trade_book():
    """The order book has no fill quantity, so remaining must come from the
    trade book's fldQty for the same nOrdNo."""
    orders = [
        {"nOrdNo": "A1", "trnsTp": "S", "ordSt": "open", "qty": "75",
         "trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML"},
    ]
    trades = [{"nOrdNo": "A1", "fldQty": "25"}]
    working = order_api._working_sell_orders(orders, trades)
    assert working[("NIFTY", "nse_fo", "NRML")][0]["remaining"] == 50


def test_working_sell_orders_ignores_buys_and_completed():
    orders = [
        {"nOrdNo": "B1", "trnsTp": "B", "ordSt": "open", "qty": "75"},
        {"nOrdNo": "C1", "trnsTp": "S", "ordSt": "complete", "qty": "75"},
    ]
    assert order_api._working_sell_orders(orders, []) == {}


def test_working_sell_orders_skips_fully_filled():
    orders = [{"nOrdNo": "D1", "trnsTp": "S", "ordSt": "open", "qty": "75",
               "trdSym": "X", "exSeg": "nse_fo", "prod": "NRML"}]
    trades = [{"nOrdNo": "D1", "fldQty": "75"}]
    assert order_api._working_sell_orders(orders, trades) == {}


# ---------------------------------------------------------------------------
# K1 - close_all_positions honours the broker reply
# ---------------------------------------------------------------------------


def _patch_books(monkeypatch, positions, orders=None, trades=None):
    monkeypatch.setattr(order_api, "get_positions", lambda auth: {"stat": "Ok", "data": positions})
    monkeypatch.setattr(
        order_api, "get_order_book", lambda auth: {"stat": "Ok", "data": orders or []}
    )
    monkeypatch.setattr(
        order_api, "get_trade_book", lambda auth: {"stat": "Ok", "data": trades or []}
    )
    monkeypatch.setattr(order_api, "_invalidate_position_cache", lambda auth: None)


def test_close_all_positions_reports_rejection_as_failure(monkeypatch):
    """K1: a rejected close must NOT be reported as a completed square-off."""
    _patch_books(
        monkeypatch,
        [{"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
          "flBuyQty": "75", "flSellQty": "0"}],
    )
    monkeypatch.setattr(order_api, "map_exchange", lambda e: "NFO")
    monkeypatch.setattr(order_api, "_resolve_position_symbol", lambda p, e: "NIFTY")
    monkeypatch.setattr(order_api, "reverse_map_product_type", lambda p: "NRML")

    class _Res:
        status = 200

    monkeypatch.setattr(
        order_api,
        "place_order_api",
        lambda data, auth: (_Res(), {"stat": "Not_Ok", "emsg": "Insufficient balance."}, None),
    )

    payload, status = order_api.close_all_positions("key", AUTH)
    assert status == 500
    assert payload["status"] == "error"
    assert "Insufficient balance" in payload["message"]


def test_close_all_positions_success_when_broker_accepts(monkeypatch):
    _patch_books(
        monkeypatch,
        [{"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
          "flBuyQty": "75", "flSellQty": "0"}],
    )
    monkeypatch.setattr(order_api, "map_exchange", lambda e: "NFO")
    monkeypatch.setattr(order_api, "_resolve_position_symbol", lambda p, e: "NIFTY")
    monkeypatch.setattr(order_api, "reverse_map_product_type", lambda p: "NRML")

    class _Res:
        status = 200

    monkeypatch.setattr(
        order_api,
        "place_order_api",
        lambda data, auth: (_Res(), {"stat": "Ok", "nOrdNo": "123"}, "123"),
    )

    payload, status = order_api.close_all_positions("key", AUTH)
    assert status == 200
    assert payload["status"] == "success"
    assert payload["closed"][0]["quantity"] == 75


def test_close_all_positions_refuses_when_order_book_unreadable(monkeypatch):
    """K2: without a readable order book we cannot tell whether a close is
    already working, so a sweep would risk overselling. Refuse."""
    monkeypatch.setattr(
        order_api,
        "get_positions",
        lambda auth: {"stat": "Ok", "data": [
            {"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
             "flBuyQty": "75", "flSellQty": "0"}]},
    )
    monkeypatch.setattr(
        order_api, "get_order_book",
        lambda auth: {"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003},
    )
    monkeypatch.setattr(order_api, "get_trade_book", lambda auth: {"stat": "Ok", "data": []})
    monkeypatch.setattr(order_api, "_invalidate_position_cache", lambda auth: None)

    payload, status = order_api.close_all_positions("key", AUTH)
    assert status == 500
    assert payload["status"] == "error"


def test_close_all_positions_does_not_oversell_covered_long(monkeypatch):
    """K2: a long already covered by a working SELL that could not be
    cancelled must not be sold again."""
    _patch_books(
        monkeypatch,
        [{"trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML",
          "flBuyQty": "75", "flSellQty": "0"}],
        orders=[{"nOrdNo": "S1", "trnsTp": "S", "ordSt": "open", "qty": "75",
                 "trdSym": "NIFTY", "exSeg": "nse_fo", "prod": "NRML"}],
    )
    monkeypatch.setattr(order_api, "map_exchange", lambda e: "NFO")
    monkeypatch.setattr(order_api, "_resolve_position_symbol", lambda p, e: "NIFTY")
    monkeypatch.setattr(order_api, "reverse_map_product_type", lambda p: "NRML")
    # The cancel fails, so the working SELL still covers the long.
    monkeypatch.setattr(
        order_api, "cancel_order", lambda oid, auth: ({"status": "error"}, 500)
    )

    placed = []
    monkeypatch.setattr(
        order_api,
        "place_order_api",
        lambda data, auth: placed.append(data) or (None, {"stat": "Ok"}, "x"),
    )

    payload, status = order_api.close_all_positions("key", AUTH)
    assert status == 200
    assert placed == [], "must not place a second SELL over a covered long"
    assert payload["covered"][0]["working_sell_qty"] == 75


# ---------------------------------------------------------------------------
# K3 - symbol resolution without a documented `tok`
# ---------------------------------------------------------------------------


def test_resolve_position_symbol_without_tok(monkeypatch):
    """`tok` is not a documented positions field; resolution must fall back to
    the trading symbol instead of raising KeyError."""
    monkeypatch.setattr(order_api, "get_symbol", lambda t, e: None)
    monkeypatch.setattr(order_api, "get_oa_symbol", lambda s, e: "NIFTY")
    assert order_api._resolve_position_symbol({"trdSym": "NIFTY"}, "NFO") == "NIFTY"


def test_resolve_position_symbol_returns_none_when_unknown(monkeypatch):
    monkeypatch.setattr(order_api, "get_symbol", lambda t, e: None)
    monkeypatch.setattr(order_api, "get_oa_symbol", lambda s, e: None)
    assert order_api._resolve_position_symbol({"trdSym": "???"}, "NFO") is None


# ---------------------------------------------------------------------------
# K14 - flip clamp
# ---------------------------------------------------------------------------


def test_smartorder_clamps_flip_to_flat(monkeypatch):
    """A target on the far side of zero must not become an order twice the
    position size (long 75 -> target -75 would SELL 150)."""
    monkeypatch.setattr(order_api, "_invalidate_position_cache", lambda auth: None)
    monkeypatch.setattr(order_api, "get_open_position", lambda s, e, p, a: "75")
    monkeypatch.setattr(order_api, "map_product_type", lambda p: "NRML")

    placed = []
    monkeypatch.setattr(
        order_api,
        "place_order_api",
        lambda data, auth: placed.append(data) or (None, {"stat": "Ok"}, "x"),
    )

    order_api.place_smartorder_api(
        {"symbol": "NIFTY", "exchange": "NFO", "product": "NRML",
         "position_size": "-75", "quantity": "75", "action": "BUY"},
        AUTH,
    )
    # Clamped to flat: SELL 75 to close, never SELL 150.
    assert placed[0]["action"] == "SELL"
    assert placed[0]["quantity"] == "75"


def test_smartorder_allows_flip_when_opted_in(monkeypatch):
    monkeypatch.setattr(order_api, "_invalidate_position_cache", lambda auth: None)
    monkeypatch.setattr(order_api, "get_open_position", lambda s, e, p, a: "75")
    monkeypatch.setattr(order_api, "map_product_type", lambda p: "NRML")

    placed = []
    monkeypatch.setattr(
        order_api,
        "place_order_api",
        lambda data, auth: placed.append(data) or (None, {"stat": "Ok"}, "x"),
    )

    order_api.place_smartorder_api(
        {"symbol": "NIFTY", "exchange": "NFO", "product": "NRML",
         "position_size": "-75", "quantity": "75", "action": "BUY",
         "allow_flip": "true"},
        AUTH,
    )
    assert placed[0]["action"] == "SELL"
    assert placed[0]["quantity"] == "150"


# ---------------------------------------------------------------------------
# K7-K10 - order payload mapping
# ---------------------------------------------------------------------------


def test_retention_accepts_day_and_ioc_only():
    assert td._retention({"validity": "DAY"}) == "DAY"
    assert td._retention({"validity": "ioc"}) == "IOC"
    assert td._retention({}) == "DAY"
    assert td._retention({"validity": "EOS"}) == "DAY"


def test_map_order_type_raises_on_unknown():
    """K8: the old default was the literal 'MARKET', which is not a documented
    Kotak `pt` value - it produced a payload Kotak rejected."""
    assert td.map_order_type("MARKET") == "MKT"
    assert td.map_order_type("LIMIT") == "L"
    with pytest.raises(ValueError):
        td.map_order_type("BOGUS")


def test_product_type_maps_mtf_and_co_bo():
    """K9/K10: MTF was unmapped (pc: null); CO/BO are discontinued for new
    orders but must still map so a pre-cutover position can be closed."""
    assert td.map_product_type("MTF") == "MTF"
    assert td.map_product_type("CO") == "CO"
    assert td.map_product_type("BO") == "BO"
    assert td.reverse_map_product_type("CO") == "CO"
    assert td.reverse_map_product_type("MTF") == "MTF"


# ---------------------------------------------------------------------------
# K18 - documented error spelling
# ---------------------------------------------------------------------------


def test_error_reply_uses_documented_spelling():
    """Every Kotak endpoint documents 'Not_Ok' with the underscore."""
    assert order_api._error_reply("boom")["stat"] == "Not_Ok"
    assert order_api._error_reply("boom")["emsg"] == "boom"


# ---------------------------------------------------------------------------
# K16 - rate limiter
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_burst_then_throttles():
    from broker.kotak.api import rate_limit

    rate_limit.reset_rate_limit()
    # A full window's worth of slots must be free immediately.
    for _ in range(rate_limit.KOTAK_MAX_REQUESTS_PER_SECOND):
        assert rate_limit.reserve_slot() == 0.0
    # The next one must wait.
    assert rate_limit.reserve_slot() > 0.0
    rate_limit.reset_rate_limit()


def test_read_book_treats_live_flat_positions_reply_as_empty():
    """K4 regression, live shape: a flat Kotak account answers /positions with
    {'stCode': 5203, 'errMsg': 'No Data', 'desc': 'data not found',
     'stat': 'Not_Ok'} — the LIVE reply uses errMsg/desc, not the doc's emsg.
    That shape must read as EMPTY ([]), not raise BrokerReadError, or the funds
    PnL path falls back to API-provided numbers on every poll with a false
    'read failed' warning."""
    live_flat_reply = {
        "stCode": 5203,
        "errMsg": "No Data",
        "desc": "data not found",
        "stat": "Not_Ok",
    }
    assert order_api._is_empty_book_response(live_flat_reply) is True
    assert order_api._read_book(live_flat_reply, "Kotak position book") == []
    # The documented emsg spelling still reads empty.
    assert order_api._read_book(
        {"stat": "Not_Ok", "emsg": "no data"}, "book"
    ) == []
    # A non-empty Not_Ok must STILL raise.
    import pytest as _pytest

    with _pytest.raises(order_api.BrokerReadError):
        order_api._read_book(
            {"stat": "Not_Ok", "emsg": "Session Expired"}, "book"
        )
