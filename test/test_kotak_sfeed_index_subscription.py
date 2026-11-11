"""
Kotak SFeed index-subscription behaviour.

Guards the fix where index symbols (NIFTY on NSE_INDEX) are subscribed through
the SFeed index feed by NAME ("nse_cm|Nifty 50", sub_type "ifs") instead of as
ordinary scrips by master-contract token. Subscribing an index as an ordinary
scrip is accepted by Kotak and then never ticks, which is what left index
charts empty after the HSM -> SFeed migration.
"""

import threading

# Import the websocket_proxy package BEFORE the kotak adapter: its
# __init__ imports every broker adapter, so importing kotak_adapter first
# would hit the circular path (kotak_adapter -> websocket_proxy.base_adapter
# -> websocket_proxy.__init__ -> kotak_adapter) mid-initialization.
import websocket_proxy  # noqa: F401

from broker.kotak.streaming.kotak_adapter import (
    KotakWebSocketAdapter,
    index_name_candidates,
    is_index_exchange,
)


class DummyWebSocket:
    @staticmethod
    def is_connected():
        return False

    @staticmethod
    def close():
        pass


def make_adapter():
    """A bare adapter instance with just the state subscribe_quote touches."""
    adapter = KotakWebSocketAdapter.__new__(KotakWebSocketAdapter)
    adapter._ws_client = DummyWebSocket()
    adapter._lock = threading.RLock()
    adapter._subscription_queue = []
    adapter._batch_timer = None
    adapter._batch_delay = 0.05
    adapter._max_batch_size = 100
    adapter._kotak_to_openalgo = {}
    adapter._symbol_modes = {}
    adapter._symbol_state = {}
    adapter._ltp_cache = {}
    adapter._quote_cache = {}
    adapter._depth_cache = {}
    adapter._depth_poll_state = {}
    adapter._index_alias_targets = {}
    adapter.subscriptions = {}
    return adapter


def test_is_index_exchange():
    assert is_index_exchange("NSE_INDEX")
    assert is_index_exchange("BSE_INDEX")
    assert not is_index_exchange("NSE")
    assert not is_index_exchange("NFO")


def test_index_name_candidates():
    assert index_name_candidates("NIFTY") == ["Nifty 50"]
    assert index_name_candidates("nifty") == ["Nifty 50"]
    assert index_name_candidates("BANKNIFTY") == ["Nifty Bank"]
    # Unknown symbols fall back to the symbol itself.
    assert index_name_candidates("RELIANCE") == ["RELIANCE"]


def test_subscribe_quote_index_uses_index_feed_name(monkeypatch):
    monkeypatch.setattr("database.token_db.get_token", lambda symbol, exchange: "26000")
    adapter = make_adapter()

    ok = adapter.subscribe_quote("NSE_INDEX", "NIFTY", 2)
    assert ok is True

    # One queued subscription: the index NAME on the index feed, not the token.
    assert len(adapter._subscription_queue) == 1
    queued = adapter._subscription_queue[0]
    assert queued["kotak_exchange"] == "nse_cm"
    assert queued["token"] == "Nifty 50"
    assert queued["sub_type"] == "ifs"

    # Inbound identity is registered under both the token and the name.
    assert adapter._kotak_to_openalgo[("nse_cm", "26000")] == ("NSE_INDEX", "NIFTY")
    assert adapter._kotak_to_openalgo[("nse_cm", "Nifty 50")] == ("NSE_INDEX", "NIFTY")
    assert adapter._index_alias_targets[("nse_cm", "Nifty 50")] == {
        ("NSE_INDEX", "NIFTY"): "26000"
    }
    # The alias shares the token's mode set.
    assert adapter._symbol_modes[("nse_cm", "Nifty 50")] is adapter._symbol_modes[
        ("nse_cm", "26000")
    ]


def test_subscribe_quote_equity_still_uses_token(monkeypatch):
    monkeypatch.setattr("database.token_db.get_token", lambda symbol, exchange: "2885")
    adapter = make_adapter()

    ok = adapter.subscribe_quote("NSE", "RELIANCE", 2)
    assert ok is True

    queued = adapter._subscription_queue[0]
    assert queued["kotak_exchange"] == "nse_cm"
    assert queued["token"] == "2885"
    assert queued["sub_type"] == "mws"


def test_index_tick_fans_out_to_registered_symbol(monkeypatch):
    monkeypatch.setattr("database.token_db.get_token", lambda symbol, exchange: "26000")
    adapter = make_adapter()
    adapter.subscribe_quote("NSE_INDEX", "NIFTY", 2)

    published = []

    def fake_publish(topic, data):
        published.append((topic, data))

    adapter.publish_market_data = fake_publish

    # SFeed index packet: Kotak's own token, name as ts, ltp present.
    tick = {
        "tk": "4247863880",
        "e": "nse_cm",
        "ts": "Nifty 50",
        "ltp": 24000.5,
        "open": 23950.0,
        "high": 24010.0,
        "low": 23940.0,
        "prev_close": 23900.0,
        "volume": 0,
    }
    adapter._on_data_received(tick)

    assert any(topic == "NSE_INDEX_NIFTY_QUOTE" for topic, _ in published)
    quote = next(data for topic, data in published if topic == "NSE_INDEX_NIFTY_QUOTE")
    assert quote["ltp"] == 24000.5
    assert quote["symbol"] == "NIFTY"
    assert quote["exchange"] == "NSE_INDEX"
