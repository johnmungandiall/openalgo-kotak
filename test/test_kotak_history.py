"""Tests for the Kotak historical-data fallback (PR marketcalls/openalgo#1466).

Kotak Neo's official API has no candle endpoint, so `get_history` sources
candles from the free Yahoo Finance chart API for indices and cash equities.
Option/future legs have no free source and must degrade to an empty frame.

No test here touches the network: the HTTP client is monkeypatched, so these
pin the mapping, the IST window arithmetic and the parsing rules without
depending on Yahoo being reachable.

Run: .venv/Scripts/python.exe -m pytest test/test_kotak_history.py -q
"""

import pandas as pd
import pytest

from broker.kotak.api import data as kotak_data

AUTH = "a:::b:::https://example.invalid:::d"


@pytest.fixture
def broker():
    return kotak_data.BrokerData(AUTH)


# ---------------------------------------------------------------------------
# Ticker mapping
# ---------------------------------------------------------------------------


def test_index_tickers_map(broker):
    assert broker._yahoo_ticker("NIFTY", "NSE_INDEX") == "^NSEI"
    assert broker._yahoo_ticker("BANKNIFTY", "NSE_INDEX") == "^NSEBANK"
    assert broker._yahoo_ticker("SENSEX", "BSE_INDEX") == "^BSESN"


def test_index_symbol_on_plain_exchange_still_maps(broker):
    """A caller may pass an index symbol on a plain NSE/BSE exchange."""
    assert broker._yahoo_ticker("NIFTY", "NSE") == "^NSEI"


def test_cash_equity_tickers_map(broker):
    assert broker._yahoo_ticker("RELIANCE", "NSE") == "RELIANCE.NS"
    assert broker._yahoo_ticker("TCS", "BSE") == "TCS.BO"


def test_fno_and_unmapped_return_none(broker):
    """Option/future legs, currency and commodities have no free source."""
    assert broker._yahoo_ticker("NIFTY26NOV25000CE", "NFO") is None
    assert broker._yahoo_ticker("USDINR", "CDS") is None
    assert broker._yahoo_ticker("CRUDEOIL", "MCX") is None
    assert broker._yahoo_ticker("SOMETHINGELSE", "NSE_INDEX") is None


# ---------------------------------------------------------------------------
# IST window arithmetic
# ---------------------------------------------------------------------------


def test_ist_midnight_epoch_start(broker):
    # 2026-11-01 00:00 IST == 2026-10-31 18:30 UTC
    assert broker._ist_midnight_epoch("2026-11-01") == 1793471400


def test_ist_midnight_epoch_end_is_inclusive(broker):
    """end=True returns the START of the next day, so the end date is included."""
    start_of_day = broker._ist_midnight_epoch("2026-11-10")
    next_day = broker._ist_midnight_epoch("2026-11-10", end=True)
    assert next_day - start_of_day == 86400


def test_ist_midnight_epoch_accepts_date_and_datetime(broker):
    from datetime import date, datetime

    expected = broker._ist_midnight_epoch("2026-11-01")
    assert broker._ist_midnight_epoch(date(2026, 11, 1)) == expected
    assert broker._ist_midnight_epoch(datetime(2026, 11, 1, 15, 30)) == expected


# ---------------------------------------------------------------------------
# get_history parsing (HTTP mocked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def _yahoo_payload(timestamps, opens, highs, lows, closes, volumes):
    return {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _patch_client(monkeypatch, response):
    class _Client:
        def get(self, *args, **kwargs):
            return response

    monkeypatch.setattr(kotak_data, "get_httpx_client", lambda: _Client())


def test_get_history_parses_candles(broker, monkeypatch):
    payload = _yahoo_payload(
        timestamps=[1793677500, 1793763900],
        opens=[23582.25, 23742.25],
        highs=[23810.85, 23960.60],
        lows=[23482.80, 23696.15],
        closes=[23810.85, 23960.60],
        volumes=[408400, 398400],
    )
    _patch_client(monkeypatch, _FakeResponse(payload))

    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")

    assert len(df) == 2
    assert list(df.columns) == [
        "timestamp", "open", "high", "low", "close", "volume", "oi",
    ]
    assert df["close"].iloc[0] == pytest.approx(23810.85)
    assert df["oi"].tolist() == [0, 0]


def test_get_history_skips_null_gap_candles(broker, monkeypatch):
    """Yahoo emits null OHLC for gap/holiday candles; those rows are dropped."""
    payload = _yahoo_payload(
        timestamps=[1793677500, 1793763900],
        opens=[23582.25, None],
        highs=[23810.85, None],
        lows=[23482.80, None],
        closes=[23810.85, None],
        volumes=[408400, None],
    )
    _patch_client(monkeypatch, _FakeResponse(payload))

    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert len(df) == 1


def test_get_history_dedupes_and_sorts(broker, monkeypatch):
    payload = _yahoo_payload(
        timestamps=[1793763900, 1793677500, 1793763900],
        opens=[2.0, 1.0, 2.0],
        highs=[2.0, 1.0, 2.0],
        lows=[2.0, 1.0, 2.0],
        closes=[2.0, 1.0, 2.0],
        volumes=[20, 10, 20],
    )
    _patch_client(monkeypatch, _FakeResponse(payload))

    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert df["timestamp"].tolist() == [1793677500, 1793763900]


def test_get_history_clamps_to_requested_window(broker, monkeypatch):
    """Yahoo can return candles slightly outside the window; they are dropped."""
    payload = _yahoo_payload(
        timestamps=[1000000000, 1793677500],
        opens=[1.0, 2.0],
        highs=[1.0, 2.0],
        lows=[1.0, 2.0],
        closes=[1.0, 2.0],
        volumes=[1, 2],
    )
    _patch_client(monkeypatch, _FakeResponse(payload))

    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert df["timestamp"].tolist() == [1793677500]


def test_get_history_option_leg_returns_empty(broker, monkeypatch):
    """An option leg must degrade to empty, never fetch the wrong instrument."""
    called = []

    class _Client:
        def get(self, *args, **kwargs):
            called.append(True)
            return _FakeResponse({})

    monkeypatch.setattr(kotak_data, "get_httpx_client", lambda: _Client())

    df = broker.get_history("NIFTY26NOV25000CE", "NFO", "D", "2026-11-01", "2026-11-10")
    assert df.empty
    assert called == [], "must not issue a request for an unmapped instrument"


def test_get_history_unsupported_interval_returns_empty(broker, monkeypatch):
    called = []

    class _Client:
        def get(self, *args, **kwargs):
            called.append(True)
            return _FakeResponse({})

    monkeypatch.setattr(kotak_data, "get_httpx_client", lambda: _Client())

    df = broker.get_history("NIFTY", "NSE_INDEX", "7m", "2026-11-01", "2026-11-10")
    assert df.empty
    assert called == []


def test_get_history_http_error_returns_empty(broker, monkeypatch):
    _patch_client(monkeypatch, _FakeResponse({"err": "nope"}, status_code=429))
    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert df.empty


def test_get_history_yahoo_error_payload_returns_empty(broker, monkeypatch):
    _patch_client(
        monkeypatch,
        _FakeResponse({"chart": {"result": None, "error": {"code": "Not Found"}}}),
    )
    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert df.empty


def test_get_history_bad_date_returns_empty(broker, monkeypatch):
    _patch_client(monkeypatch, _FakeResponse({}))
    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "not-a-date", "2026-11-10")
    assert df.empty


def test_get_history_empty_result_returns_empty(broker, monkeypatch):
    _patch_client(monkeypatch, _FakeResponse({"chart": {"result": [], "error": None}}))
    df = broker.get_history("NIFTY", "NSE_INDEX", "D", "2026-11-01", "2026-11-10")
    assert df.empty


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------


def test_timeframe_map_covers_documented_intervals(broker):
    assert broker.timeframe_map == {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "60m",
        "D": "1d",
    }


def test_get_supported_intervals_shape(broker):
    intervals = broker.get_supported_intervals()
    assert intervals["minutes"] == ["1m", "5m", "15m", "30m"]
    assert intervals["hours"] == ["1h"]
    assert intervals["days"] == ["D"]
    assert intervals["seconds"] == []
    assert intervals["weeks"] == []
    assert intervals["months"] == []


def test_intervals_service_derives_from_timeframe_map(monkeypatch):
    """services/intervals_service.py reads timeframe_map, not
    get_supported_intervals - so the map is what makes /intervals work."""
    from services import intervals_service

    ok, payload, code = intervals_service.get_intervals_with_auth(AUTH, "kotak")
    assert ok is True
    assert code == 200
    data = payload["data"]
    assert data["minutes"] == ["1m", "5m", "15m", "30m"]
    assert data["hours"] == ["1h"]
    assert data["days"] == ["D"]
