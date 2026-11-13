"""
Live-path /api/v1/optionchain shape against the Qwantonomous Engine
contract (docs/openalgo-api-contract.md §2.5).

The engine's OptionSymbolsFetchService reads `chain` rows, each with a `ce`
and a `pe` object carrying at least `symbol`, `ltp` and `oi` (rows with
`oi <= 0` or `ltp <= 0` are skipped by the engine), plus the top-level
`atm_strike` and `underlying_ltp`. The live broker path used to emit
`"ce": null` / `"pe": null` for legs missing from the master contract, so
the leg keys were not canonical. This pins the fix.
"""

import pytest

from services import option_chain_service as svc


def _accessible_symbols(base_symbol, expiry_date, strikes_with_labels, exchange):
    out = []
    for s in strikes_with_labels:
        strike = s["strike"]
        out.append(
            {
                "strike": strike,
                "ce": {
                    "symbol": f"{base_symbol}{expiry_date}{strike:.0f}CE",
                    "label": s["ce_label"],
                    "exists": True,
                    "lotsize": 25,
                    "tick_size": 0.05,
                },
                "pe": {
                    "symbol": f"{base_symbol}{expiry_date}{strike:.0f}PE",
                    "label": s["pe_label"],
                    "exists": False,  # simulate a leg missing from the DB
                    "lotsize": 25,
                    "tick_size": 0.05,
                },
            }
        )
    return out


_OI_BY_SYMBOL = {"NIFTY12MAY2624000CE": 12345, "NIFTY12MAY2624500CE": 9876}


def _fake_multiquotes(symbols, api_key=None, auth_token=None, feed_token=None, broker=None):
    results = []
    for item in symbols:
        sym, exch = item["symbol"], item["exchange"]
        data = {
            "bid": 0,
            "ask": 0,
            "open": 0,
            "high": 0,
            "low": 0,
            "ltp": 120.5 if sym.endswith("CE") else 0,
            "prev_close": 0,
            "volume": 0,
            "oi": _OI_BY_SYMBOL.get(sym, 0),
            "tick_size": 0.05,
        }
        results.append({"symbol": sym, "exchange": exch, "data": data})
    return True, {"status": "success", "results": results}, 200


@pytest.fixture(autouse=True)
def _patch_dependencies(monkeypatch):
    monkeypatch.setattr(
        svc, "get_auth_token_broker",
        lambda api_key, include_feed_token=False: ("fake-token", None, "flattrade"),
    )
    monkeypatch.setattr(
        svc, "get_quotes",
        lambda symbol, exchange, api_key=None: (True, {"data": {"ltp": 24250.5, "prev_close": 24200.0}}, 200),
    )
    monkeypatch.setattr(svc, "get_option_exchange", lambda quote_exchange: "NFO")
    monkeypatch.setattr(
        svc, "get_available_strikes",
        lambda base_symbol, expiry, opt_type, exchange: [24000.0, 24250.0, 24500.0],
    )
    monkeypatch.setattr(
        svc, "find_atm_strike_from_actual",
        lambda underlying_ltp, available_strikes: 24250.0,
    )
    monkeypatch.setattr(svc, "get_option_symbols_for_chain", _accessible_symbols)
    monkeypatch.setattr(svc, "get_multiquotes", _fake_multiquotes)


def test_optionchain_engine_contract_shape():
    success, response, status = svc.get_option_chain(
        underlying="NIFTY",
        exchange="NSE_INDEX",
        expiry_date="12MAY26",
        strike_count=1,
        api_key="key",
    )

    assert success is True
    assert status == 200
    assert response["status"] == "success"
    assert isinstance(response["atm_strike"], (int, float))
    assert isinstance(response["underlying_ltp"], (int, float))
    assert isinstance(response["chain"], list)
    assert len(response["chain"]) == 3

    for row in response["chain"]:
        assert isinstance(row["strike"], (int, float))
        for leg_key in ("ce", "pe"):
            leg = row[leg_key]
            # Every leg is an object with canonical keys — never None.
            assert isinstance(leg, dict), f"{leg_key} leg must be an object"
            assert isinstance(leg["symbol"], str) and leg["symbol"]
            assert isinstance(leg["ltp"], (int, float))
            assert isinstance(leg["oi"], (int, float))
            # Keys the engine / chain dialog read or display.
            for key in ("label", "bid", "ask", "open", "high", "low", "volume",
                        "lotsize", "tick_size"):
                assert key in leg, f"leg missing canonical key {key}"

    # The DB-missing PE leg still carries an object (with zero prices), so the
    # chain stays N-in → N-out with canonical keys on every row.
    missing_leg_row = response["chain"][0]["pe"]
    assert missing_leg_row["ltp"] == 0
    assert missing_leg_row["oi"] == 0
    assert missing_leg_row["symbol"] == "NIFTY12MAY2624000PE"
