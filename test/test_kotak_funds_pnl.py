"""Tests for the Kotak funds PnL fix (PR marketcalls/openalgo#1224).

Kotak's limits reply carries `RealizedMtomPrsnt` / `UnrealizedMtomPrsnt`, but its
`rpnl` is inaccurate for closed positions, so PnL is computed from the position
book instead. The broker's own MTOM fields remain the fallback for every case
where the book cannot be used.

The arithmetic is the part that can silently produce a wrong number, so each
branch is pinned here: fully closed, partial long, partial short, and the three
fallback paths.

No test touches the network or the database: the position book, the symbol
lookup and the quote fetch are all monkeypatched.

Run: .venv/Scripts/python.exe -m pytest test/test_kotak_funds_pnl.py -q
"""

import pytest

from broker.kotak.api import funds

AUTH = "trade-token:::trade-sid:::https://example.invalid:::access-token"


# ---------------------------------------------------------------------------
# safe_float / safe_int
# ---------------------------------------------------------------------------


def test_safe_float_handles_kotak_string_numbers():
    """Kotak returns every numeric field as a string, and nulls the ones it has
    no value for. A bare float() on those raises."""
    assert funds.safe_float("12.5") == 12.5
    assert funds.safe_float("0") == 0.0
    assert funds.safe_float(None) == 0.0
    assert funds.safe_float("") == 0.0
    assert funds.safe_float("abc") == 0.0
    assert funds.safe_float("abc", default=-1.0) == -1.0


def test_safe_int_handles_kotak_string_numbers():
    assert funds.safe_int("75") == 75
    assert funds.safe_int("75.0") == 75
    assert funds.safe_int(None) == 0
    assert funds.safe_int("") == 0
    assert funds.safe_int("abc") == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position(**overrides):
    """A Kotak position row with sane defaults; override per test."""
    row = {
        "trdSym": "NIFTY",
        "exSeg": "nse_fo",
        "prod": "NRML",
        "flBuyQty": "0",
        "flSellQty": "0",
        "cfBuyQty": "0",
        "cfSellQty": "0",
        "buyAmt": "0",
        "sellAmt": "0",
        "cfBuyAmt": "0",
        "cfSellAmt": "0",
    }
    row.update(overrides)
    return row


def _patch(monkeypatch, positions, ltp=0.0, quotes_error=None):
    """Wire the position book, symbol lookup and quote fetch."""
    from broker.kotak.api import order_api

    monkeypatch.setattr(order_api, "get_positions", lambda auth: {"stat": "Ok", "data": positions})
    monkeypatch.setattr(funds, "_position_symbol", lambda p: ("NIFTY", "NFO"))

    class _BrokerData:
        def __init__(self, auth_token):
            pass

        def get_multiquotes(self, symbols):
            if quotes_error:
                raise quotes_error
            return [
                {"symbol": s["symbol"], "exchange": s["exchange"], "data": {"ltp": ltp}}
                for s in symbols
            ]

    import broker.kotak.api.data as kotak_data

    monkeypatch.setattr(kotak_data, "BrokerData", _BrokerData)


# ---------------------------------------------------------------------------
# Realized PnL
# ---------------------------------------------------------------------------


def test_closed_position_realized_is_sell_minus_buy(monkeypatch):
    """A fully closed position: the amount difference IS the realized PnL."""
    _patch(
        monkeypatch,
        [
            _position(
                flBuyQty="75", flSellQty="75",
                buyAmt="100000", sellAmt="102500",
            )
        ],
    )
    realised, unrealised = funds._pnl_from_positions(AUTH, {})
    assert realised == pytest.approx(2500.0)
    assert unrealised == pytest.approx(0.0)


def test_closed_position_includes_carry_forward(monkeypatch):
    """Carry-forward amounts must be added, or a CF position reads as flat."""
    _patch(
        monkeypatch,
        [
            _position(
                flBuyQty="50", flSellQty="50",
                buyAmt="50000", sellAmt="51000",
                cfBuyQty="25", cfSellQty="25",
                cfBuyAmt="25000", cfSellAmt="26000",
            )
        ],
    )
    realised, _ = funds._pnl_from_positions(AUTH, {})
    assert realised == pytest.approx(2000.0)  # (51000+26000) - (50000+25000)


def test_partial_long_realizes_only_the_closed_portion(monkeypatch):
    """Net long 50 of 100 bought, 50 sold: realize the sold half only."""
    _patch(
        monkeypatch,
        [
            _position(
                flBuyQty="100", flSellQty="50",
                buyAmt="100000", sellAmt="55000",
            )
        ],
        ltp=1100.0,
    )
    realised, unrealised = funds._pnl_from_positions(AUTH, {})
    # avg buy = 1000; realized = 55000 - (1000 * 50) = 5000
    assert realised == pytest.approx(5000.0)
    # unrealized = (1100 - 1000) * 50 = 5000
    assert unrealised == pytest.approx(5000.0)


def test_partial_short_realizes_only_the_closed_portion(monkeypatch):
    """Net short: the buy-back is the closed portion, priced off the sell avg."""
    _patch(
        monkeypatch,
        [
            _position(
                flBuyQty="50", flSellQty="100",
                buyAmt="45000", sellAmt="100000",
            )
        ],
        ltp=800.0,
    )
    realised, unrealised = funds._pnl_from_positions(AUTH, {})
    # avg sell = 1000; realized = (1000 * 50) - 45000 = 5000
    assert realised == pytest.approx(5000.0)
    # net -50; unrealized = (800 - 1000) * -50 = 10000
    assert unrealised == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# Unrealized PnL
# ---------------------------------------------------------------------------


def test_open_long_unrealized_uses_buy_average(monkeypatch):
    _patch(
        monkeypatch,
        [_position(flBuyQty="100", buyAmt="100000")],
        ltp=1100.0,
    )
    realised, unrealised = funds._pnl_from_positions(AUTH, {})
    assert realised == pytest.approx(0.0)
    assert unrealised == pytest.approx(10000.0)  # (1100-1000)*100


def test_open_short_unrealized_uses_sell_average(monkeypatch):
    _patch(
        monkeypatch,
        [_position(flSellQty="100", sellAmt="100000")],
        ltp=900.0,
    )
    _, unrealised = funds._pnl_from_positions(AUTH, {})
    assert unrealised == pytest.approx(10000.0)  # (900-1000)*-100


def test_multiple_positions_are_summed(monkeypatch):
    _patch(
        monkeypatch,
        [
            _position(flBuyQty="100", buyAmt="100000"),
            _position(flBuyQty="100", buyAmt="100000"),
        ],
        ltp=1100.0,
    )
    _, unrealised = funds._pnl_from_positions(AUTH, {})
    assert unrealised == pytest.approx(20000.0)


# ---------------------------------------------------------------------------
# Fallbacks — a wrong number is worse than the broker's own number
# ---------------------------------------------------------------------------


def test_unreadable_book_falls_back_to_api_values(monkeypatch):
    """An unreadable book must NOT read as zero PnL."""
    from broker.kotak.api import order_api

    monkeypatch.setattr(
        order_api,
        "get_positions",
        lambda auth: {"stat": "Not_Ok", "emsg": "Invalid session", "stCode": 1003},
    )
    margin = {"RealizedMtomPrsnt": "1234.50", "UnrealizedMtomPrsnt": "-99.25"}
    realised, unrealised = funds._pnl_from_positions(AUTH, margin)
    assert realised == pytest.approx(1234.50)
    assert unrealised == pytest.approx(-99.25)


def test_empty_book_falls_back_to_api_values(monkeypatch):
    _patch(monkeypatch, [])
    margin = {"RealizedMtomPrsnt": "500", "UnrealizedMtomPrsnt": "250"}
    realised, unrealised = funds._pnl_from_positions(AUTH, margin)
    assert realised == pytest.approx(500.0)
    assert unrealised == pytest.approx(250.0)


def test_no_usable_ltp_falls_back_to_api_unrealized(monkeypatch):
    """Open positions but not one could be priced: the position-based
    unrealized figure is meaningless, so use the broker's."""
    _patch(
        monkeypatch,
        [_position(flBuyQty="100", buyAmt="100000")],
        ltp=0.0,
    )
    margin = {"RealizedMtomPrsnt": "0", "UnrealizedMtomPrsnt": "777.77"}
    realised, unrealised = funds._pnl_from_positions(AUTH, margin)
    assert unrealised == pytest.approx(777.77)
    # Realized is still computed from the book - only unrealized falls back.
    assert realised == pytest.approx(0.0)


def test_quote_fetch_exception_falls_back_to_api_unrealized(monkeypatch):
    _patch(
        monkeypatch,
        [_position(flBuyQty="100", buyAmt="100000")],
        quotes_error=RuntimeError("quotes down"),
    )
    margin = {"RealizedMtomPrsnt": "0", "UnrealizedMtomPrsnt": "321.00"}
    _, unrealised = funds._pnl_from_positions(AUTH, margin)
    assert unrealised == pytest.approx(321.00)


def test_partial_ltp_coverage_keeps_position_based_unrealized(monkeypatch):
    """If SOME open positions could be priced, the computed figure stands -
    falling back would discard the positions that did work."""
    from broker.kotak.api import order_api

    monkeypatch.setattr(
        order_api,
        "get_positions",
        lambda auth: {
            "stat": "Ok",
            "data": [
                _position(trdSym="AAA", flBuyQty="100", buyAmt="100000"),
                _position(trdSym="BBB", flBuyQty="100", buyAmt="100000"),
            ],
        },
    )
    # AAA resolves and prices; BBB does not resolve at all.
    monkeypatch.setattr(
        funds,
        "_position_symbol",
        lambda p: ("AAA", "NFO") if p.get("trdSym") == "AAA" else (None, None),
    )

    class _BrokerData:
        def __init__(self, auth_token):
            pass

        def get_multiquotes(self, symbols):
            return [{"symbol": "AAA", "exchange": "NFO", "data": {"ltp": 1100.0}}]

    import broker.kotak.api.data as kotak_data

    monkeypatch.setattr(kotak_data, "BrokerData", _BrokerData)

    margin = {"RealizedMtomPrsnt": "0", "UnrealizedMtomPrsnt": "99999"}
    _, unrealised = funds._pnl_from_positions(AUTH, margin)
    assert unrealised == pytest.approx(10000.0)  # AAA only, not the API value


# ---------------------------------------------------------------------------
# availablecash stays on the documented `Net` field (K5)
# ---------------------------------------------------------------------------


def test_availablecash_uses_net_not_the_undocumented_sum(monkeypatch):
    """K5: `Net` is the documented "Net available margin/cash". The old formula
    summed RmsPayInAmt / RmsPayOutAmt / Collateral, none of which the doc lists,
    so all three silently evaluated to zero."""
    _patch(monkeypatch, [])

    margin = {
        "stat": "Ok",
        "Net": "2006.59",
        "CollateralValue": "2006.59",
        "MarginUsed": "0",
        # Deliberately present and large: if the old formula were still in use,
        # availablecash would come out as 999999 instead of 2006.59.
        "RmsPayInAmt": "999999",
        "RmsPayOutAmt": "0",
        "Collateral": "0",
    }

    import broker.kotak.api.funds as funds_module

    class _Resp:
        status_code = 200
        text = __import__("json").dumps(margin)

    class _Client:
        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(funds_module, "get_httpx_client", lambda: _Client())
    monkeypatch.setattr(funds_module, "apply_rate_limit", lambda *a, **k: None)

    result = funds_module._fetch_funds(AUTH)
    assert result["availablecash"] == "2006.59"
    # `collateral` is the PLEDGED-SECURITIES figure, not the total collateral
    # value. CollateralValue equals Net on a cash-only account, so mapping it
    # here made the app's PnL (collateral + m2mrealized + m2munrealized) show
    # the account balance as profit.
    assert result["collateral"] == "0.00"


def test_collateral_is_pledged_securities_not_the_balance(monkeypatch):
    """The dashboard's PnL is `collateral + m2mrealized + m2munrealized`
    (docs/openalgo-api-contract.md §2.1). If `collateral` carried the account
    balance, PnL would display the balance as profit - which is exactly the
    defect this pins: P&L +2006.59 with both M2M legs at 0.00.

    Flattrade maps `collateral` to `brkcollamt` (pledged securities only), so
    Kotak must map to its equivalent, `Collateral`.
    """
    _patch(monkeypatch, [])

    margin = {
        "stat": "Ok",
        "Net": "2006.59",
        "CollateralValue": "2006.59",  # total collateral value == Net here
        "Collateral": "0",  # nothing pledged
        "MarginUsed": "0",
    }

    import broker.kotak.api.funds as funds_module

    class _Resp:
        status_code = 200
        text = __import__("json").dumps(margin)

    class _Client:
        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(funds_module, "get_httpx_client", lambda: _Client())
    monkeypatch.setattr(funds_module, "apply_rate_limit", lambda *a, **k: None)

    result = funds_module._fetch_funds(AUTH)

    # The app's PnL must come out at 0 for an account with no positions and no
    # pledged collateral - not at the balance.
    app_pnl = (
        float(result["collateral"])
        + float(result["m2mrealized"])
        + float(result["m2munrealized"])
    )
    assert app_pnl == pytest.approx(0.0)
    # The balance is still reported, on the field the BAL pill reads.
    assert result["availablecash"] == "2006.59"


def test_pledged_collateral_still_reported(monkeypatch):
    """When securities ARE pledged, the figure must still reach the app."""
    _patch(monkeypatch, [])

    margin = {
        "stat": "Ok",
        "Net": "5000.00",
        "CollateralValue": "8000.00",
        "Collateral": "3000.00",
        "MarginUsed": "0",
    }

    import broker.kotak.api.funds as funds_module

    class _Resp:
        status_code = 200
        text = __import__("json").dumps(margin)

    class _Client:
        def post(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(funds_module, "get_httpx_client", lambda: _Client())
    monkeypatch.setattr(funds_module, "apply_rate_limit", lambda *a, **k: None)

    result = funds_module._fetch_funds(AUTH)
    assert result["collateral"] == "3000.00"
    assert result["availablecash"] == "5000.00"
