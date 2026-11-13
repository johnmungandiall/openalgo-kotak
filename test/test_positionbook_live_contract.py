"""
Live-path /api/v1/positionbook envelope against the Qwantonomous Engine
contract (docs/openalgo-api-contract.md §2.2).

The engine's PositionBookService reads the top-level `mode`, `total_pnl`,
`total_today_realized_pnl`, `total_unrealized_pnl`, and the row fields
`lot_size`, `pnlpercent`, `today_realized_pnl`, `unrealized_pnl`. The live
broker path used to return only `{status, data}`, so the screen showed
defaults. This pins the fix.
"""

import pytest

import services.positionbook_service as pbs


def _fake_broker_module(broker_name=None):
    """Return a fake broker module dict shaped like import_broker_module's."""

    def map_position_data(position_data):
        # Minimal stand-in: no DB symbol lookup, keep rows as-is.
        return position_data

    def transform_positions_data(positions_data):
        transformed = []
        for pos in positions_data:
            realized = float(pos.get("rpnl", 0))
            unrealized = float(pos.get("urmtom", 0))
            transformed.append(
                {
                    "symbol": pos.get("tsym", ""),
                    "exchange": pos.get("exch", ""),
                    "product": pos.get("prd", ""),
                    "quantity": pos.get("netqty", 0),
                    "average_price": pos.get("netavgprc", 0.0),
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "ltp": pos.get("lp", 0.0),
                    "pnl": round(realized + unrealized, 2),
                }
            )
        return transformed

    return {
        "get_positions": lambda auth: [
            {
                "tsym": "NIFTY09JUN2623100PE",
                "exch": "NFO",
                "prd": "MIS",
                "netqty": 75,
                "netavgprc": 133.6,
                "lp": 136.15,
                "rpnl": -1074.6,
                "urmtom": 165.75,
            }
        ],
        "map_position_data": map_position_data,
        "transform_positions_data": transform_positions_data,
    }


@pytest.fixture(autouse=True)
def _no_analyze_mode(monkeypatch):
    monkeypatch.setattr("database.settings_db.get_analyze_mode", lambda: False)


def test_live_positionbook_includes_mode_totals_and_row_fields(monkeypatch):
    monkeypatch.setattr(pbs, "import_broker_module", _fake_broker_module)

    success, response, status = pbs.get_positionbook_with_auth(
        "fake-token", "flattrade", {"apikey": "key"}
    )

    assert success is True
    assert status == 200
    assert response["status"] == "success"
    assert response["mode"] == "live"

    # Totals: realized -1074.6 + unrealized 165.75 = -908.85
    assert response["total_pnl"] == pytest.approx(-908.85)
    assert response["total_today_realized_pnl"] == pytest.approx(-1074.6)
    assert response["total_unrealized_pnl"] == pytest.approx(165.75)

    rows = response["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "NIFTY09JUN2623100PE"
    assert row["quantity"] == 75
    assert row["lot_size"] == 1
    assert row["today_realized_pnl"] == pytest.approx(-1074.6)
    assert row["unrealized_pnl"] == pytest.approx(165.75)
    assert row["pnl"] == pytest.approx(-908.85)
    # pnl / (qty * avg) * 100 = -908.85 / (75 * 133.6) * 100 ≈ -9.07
    assert row["pnlpercent"] == pytest.approx(-9.07, abs=0.01)
