import pytest
from datetime import date
from unittest.mock import patch

from backend.services.stats_service import StatsService
from backend.models.live_trade import LiveTrade
from backend.models.trade import TradeSummary

@pytest.fixture
def mock_trades():
    return [
        LiveTrade(
            id="win1",
            symbol="BTCUSDT",
            side="long",
            pnl=100.0,
            result="win",
            rr=2.0,
            ts_open=date(2023, 1, 1),
            qty=1.0, entry=50000, stop=49000, tp=52000
        ),
        LiveTrade(
            id="loss1",
            symbol="ETHUSDT",
            side="short",
            pnl=-50.0,
            result="loss",
            rr=-1.0,
            ts_open=date(2023, 1, 2),
            qty=10.0, entry=2000, stop=2050, tp=1900
        ),
        LiveTrade(
            id="unknown1",
            symbol="SOLUSDT",
            side="long",
            pnl=None, # UNKNOWN
            result="unknown",
            rr=0.0,
            ts_open=date(2023, 1, 3),
            qty=50.0, entry=20, stop=19, tp=22
        )
    ]

def test_summary_ignores_unknown_pnl(mock_trades):
    with patch("backend.services.stats_service.LiveTradesService.get_trades_by_date_range", return_value=mock_trades):
        summary = StatsService.summary(date(2023, 1, 1), date(2023, 1, 3))
        
        # Win rate should be 1 / 2 = 50% (ignoring the unknown trade)
        assert summary.win_rate == 0.5
        assert summary.pnl == 50.0
        assert summary.wins == 1
        assert summary.losses == 1
        assert summary.trades == 3 # Total count includes unknown
