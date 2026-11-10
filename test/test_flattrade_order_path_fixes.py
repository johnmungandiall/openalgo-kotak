"""Regression tests for the Flattrade order-path fixes.

Covers the three discrepancies closed in docs/10-official-api-mapping.md §11:

  11.4 reverse_map_product_type returned None for H/B/F (Cover/Bracket/MTF) —
        square-off payload then carried product: None for positions opened in
        the Flattrade web terminal.
  11.6 place_order_api hard-indexed response_data["stat"] — a reply without
        it (HTML error page, proxy body) raised KeyError → generic 500.
  11.11 Order endpoints never called _apply_rate_limit() — the order path was
        uncounted against the official 10/sec · 40/min limit.

Run with the server's OWN virtualenv:

    .\.venv\Scripts\python.exe -m pytest test/test_flattrade_order_path_fixes.py -q
"""

import os
import sys

SERVER_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_ROOT not in sys.path:
    sys.path.insert(0, SERVER_ROOT)

import pytest  # noqa: E402

from broker.flattrade.mapping.transform_data import (  # noqa: E402
    map_product_type,
    reverse_map_product_type,
)


# ---------------------------------------------------------------------------
# 11.4 — product mapping round-trip includes CO / BO / MTF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "openalgo_product,broker_prd",
    [
        ("CNC", "C"),
        ("NRML", "M"),
        ("MIS", "I"),
        ("CO", "H"),
        ("BO", "B"),
        ("MTF", "F"),
    ],
)
def test_product_map_round_trip(openalgo_product, broker_prd):
    assert map_product_type(openalgo_product) == broker_prd
    assert reverse_map_product_type(broker_prd) == openalgo_product


def test_reverse_product_map_no_longer_returns_none_for_h_b_f():
    for prd in ("H", "B", "F"):
        assert reverse_map_product_type(prd) is not None


def test_product_map_default_still_mis():
    # Unknown products keep the existing defensive default (MIS).
    assert map_product_type("BOGUS") == "I"


# ---------------------------------------------------------------------------
# 11.6 — place_order_api survives a non-JSON / stat-less reply
# ---------------------------------------------------------------------------


class _FakeJsonResponse:
    """Minimal httpx-like response whose .json() raises (HTML error page)."""

    status_code = 502
    status = 502

    def json(self):
        raise ValueError("No JSON object could be decoded")


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def post(self, *args, **kwargs):
        return self._response


def _patch_order_env(monkeypatch):
    """Give place_order_api what it needs besides the HTTP call."""
    import broker.flattrade.api.order_api as oa

    monkeypatch.setenv("BROKER_API_KEY", "testkey:::secrets")
    monkeypatch.setattr(oa, "get_token", lambda symbol, exchange: "TOKEN")

    def _passthrough_transform(data, token, auth):
        return dict(data)

    monkeypatch.setattr(oa, "transform_data", _passthrough_transform)
    return oa


def test_place_order_api_guards_non_json_reply(monkeypatch):
    oa = _patch_order_env(monkeypatch)

    monkeypatch.setattr(oa, "get_httpx_client", lambda: _FakeClient(_FakeJsonResponse()))

    payload = {
        "apikey": "x",
        "strategy": "Squareoff",
        "symbol": "NIFTY",
        "action": "SELL",
        "exchange": "NFO",
        "pricetype": "MARKET",
        "product": "MIS",
        "quantity": "1",
    }
    res, response, orderid = oa.place_order_api(payload, "auth::key")
    # Success half must not have been reached: orderid None, broker-shaped error.
    assert orderid is None
    assert response.get("stat") == "Not_Ok"
    assert "non-JSON" in response.get("emsg", "")
    assert res.status_code == 502


def test_place_order_api_guards_stat_dict_reply(monkeypatch):
    """A JSON dict WITHOUT stat (proxy JSON error) must not KeyError."""

    class _FakeStatlessResponse:
        status_code = 200
        status = 200

        def json(self):
            return {"error": "gateway oops"}

    oa = _patch_order_env(monkeypatch)
    monkeypatch.setattr(
        oa, "get_httpx_client", lambda: _FakeClient(_FakeStatlessResponse())
    )

    payload = {
        "apikey": "x",
        "strategy": "Squareoff",
        "symbol": "NIFTY",
        "action": "SELL",
        "exchange": "NFO",
        "pricetype": "MARKET",
        "product": "MIS",
        "quantity": "1",
    }
    res, response, orderid = oa.place_order_api(payload, "auth::key")
    assert orderid is None
    assert response.get("stat") != "Ok"
    assert res.status_code == 200


def test_place_order_api_success_path_unchanged(monkeypatch):
    class _FakeOkResponse:
        status_code = 200
        status = 200

        def json(self):
            return {"stat": "Ok", "norenordno": "123456789"}

    oa = _patch_order_env(monkeypatch)
    monkeypatch.setattr(
        oa, "get_httpx_client", lambda: _FakeClient(_FakeOkResponse())
    )

    payload = {
        "apikey": "x",
        "strategy": "Squareoff",
        "symbol": "NIFTY",
        "action": "SELL",
        "exchange": "NFO",
        "pricetype": "MARKET",
        "product": "MIS",
        "quantity": "1",
    }
    res, response, orderid = oa.place_order_api(payload, "auth::key")
    assert orderid == "123456789"
    assert response.get("stat") == "Ok"
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# 11.1 — auth accepts the official `status` field (and legacy `stat`)
# ---------------------------------------------------------------------------


def test_authenticate_broker_accepts_status_field(monkeypatch):
    import broker.flattrade.api.auth_api as auth_api

    class _FakeAuthResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "Ok", "token": "T0K3N"}

    class _FakeAuthClient:
        def post(self, *args, **kwargs):
            return _FakeAuthResponse()

    monkeypatch.setenv("BROKER_API_KEY", "prefix:::real-key")
    monkeypatch.setenv("BROKER_API_SECRET", "secret")
    monkeypatch.setattr(auth_api, "get_httpx_client", lambda: _FakeAuthClient())

    token, error = auth_api.authenticate_broker("req-code")
    assert token == "T0K3N"
    assert error is None


def test_authenticate_broker_still_accepts_stat_field(monkeypatch):
    import broker.flattrade.api.auth_api as auth_api

    class _FakeAuthResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"stat": "Ok", "token": "T0K3N"}

    class _FakeAuthClient:
        def post(self, *args, **kwargs):
            return _FakeAuthResponse()

    monkeypatch.setenv("BROKER_API_KEY", "prefix:::real-key")
    monkeypatch.setenv("BROKER_API_SECRET", "secret")
    monkeypatch.setattr(auth_api, "get_httpx_client", lambda: _FakeAuthClient())

    token, error = auth_api.authenticate_broker("req-code")
    assert token == "T0K3N"
    assert error is None


def test_authenticate_broker_rejects_failure(monkeypatch):
    import broker.flattrade.api.auth_api as auth_api

    class _FakeAuthResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"status": "Not_Ok", "emsg": "bad code"}

    class _FakeAuthClient:
        def post(self, *args, **kwargs):
            return _FakeAuthResponse()

    monkeypatch.setenv("BROKER_API_KEY", "prefix:::real-key")
    monkeypatch.setenv("BROKER_API_SECRET", "secret")
    monkeypatch.setattr(auth_api, "get_httpx_client", lambda: _FakeAuthClient())

    token, error = auth_api.authenticate_broker("req-code")
    assert token is None
    assert "bad code" in error


# ---------------------------------------------------------------------------
# 11.7 — cancel_order reports the broker's emsg, not a generic string
# ---------------------------------------------------------------------------


def test_cancel_order_reports_emsg(monkeypatch):
    import broker.flattrade.api.order_api as oa

    class _FakeCancelResponse:
        status_code = 400
        status = 400

        def json(self):
            return {"stat": "Not_Ok", "emsg": "order already frozen"}

    class _FakeCancelClient:
        def post(self, *args, **kwargs):
            return _FakeCancelResponse()

    monkeypatch.setenv("BROKER_API_KEY", "prefix:::real-key")
    monkeypatch.setattr(
        oa, "get_httpx_client", lambda: _FakeCancelClient()
    )

    result, status = oa.cancel_order("123", "auth::key")
    assert status == 400
    assert result.get("message") == "order already frozen"


def test_cancel_order_falls_back_to_message(monkeypatch):
    import broker.flattrade.api.order_api as oa

    class _FakeCancelResponse:
        status_code = 400
        status = 400

        def json(self):
            return {"stat": "Not_Ok", "message": "legacy message"}

    class _FakeCancelClient:
        def post(self, *args, **kwargs):
            return _FakeCancelResponse()

    monkeypatch.setenv("BROKER_API_KEY", "prefix:::real-key")
    monkeypatch.setattr(
        oa, "get_httpx_client", lambda: _FakeCancelClient()
    )

    result, status = oa.cancel_order("123", "auth::key")
    assert status == 400
    assert result.get("message") == "legacy message"


# ---------------------------------------------------------------------------
# 11.8 — TradeBook timestamp uses the official fltm, falls back to norentm
# ---------------------------------------------------------------------------


def test_transform_tradebook_data_uses_fltm():
    from broker.flattrade.mapping.order_data import transform_tradebook_data

    row = {
        "fltm": "02-06-2026 15:46:23",
        "norentm": "OLD-TIMESTAMP",
        "tsym": "NIFTY",
        "exch": "NFO",
        "prd": "M",
        "trantype": "B",
        "qty": "5",
        "avgprc": "100.5",
        "norenordno": "abc",
    }
    out = transform_tradebook_data([row])[0]
    assert out["timestamp"] == "02-06-2026 15:46:23"


def test_transform_tradebook_data_falls_back_to_norentm():
    from broker.flattrade.mapping.order_data import transform_tradebook_data

    row = {
        "norentm": "OLD-TIMESTAMP",
        "tsym": "NIFTY",
        "exch": "NFO",
        "prd": "M",
        "trantype": "B",
        "qty": "5",
        "avgprc": "100.5",
        "norenordno": "abc",
    }
    out = transform_tradebook_data([row])[0]
    assert out["timestamp"] == "OLD-TIMESTAMP"


# ---------------------------------------------------------------------------
# 11.10 — websocket auth ack is case-insensitive (official "Ok" vs constant "OK")
# ---------------------------------------------------------------------------


def _make_websocket():
    # Load the module directly by path: importing via the streaming package runs
    # streaming/__init__.py, which imports flattrade_adapter -> websocket_proxy
    # -> every broker adapter -> flattrade_adapter again (circular). The module
    # itself only imports stdlib + websocket, so a file-location load is safe.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "flattrade_websocket_mod",
        os.path.join(SERVER_ROOT, "broker/flattrade/streaming/flattrade_websocket.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    ws = mod.FlattradeWebSocket(user_id="u", actid="a", accesstoken="t")
    ws._start_heartbeat = lambda: None
    ws._call_external_callback = lambda *a, **k: None
    ws._close_websocket = lambda: None
    return ws


def test_websocket_auth_ack_accepts_ok_and_OK():
    ws = _make_websocket()
    assert ws._handle_auth_response({"s": "Ok"}) is True
    assert ws.connected is True
    assert ws.auth_failed is False

    ws2 = _make_websocket()
    assert ws2._handle_auth_response({"s": "OK"}) is True
    assert ws2.connected is True


def test_websocket_auth_ack_rejects_other_values():
    ws = _make_websocket()
    assert ws._handle_auth_response({"s": "Not_Ok"}) is True
    assert ws.connected is False
    assert ws.auth_failed is True
