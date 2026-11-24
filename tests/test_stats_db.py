"""
Tests for stats endpoints — ensures all metrics come ONLY from live_trades DB table.

Test cases:
  1. test_stats_from_db_only — closed trades with PnL produce correct stats
  2. test_failed_trades_excluded — FAILED trades do not affect stats
  3. test_closed_unknown_pnl_excluded — CLOSED trades with pnl=NULL excluded
  4. test_equity_curve_and_drawdown — equity curve ordered by close time
"""

import pytest
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Ensure project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_trade(**kwargs):
    """Create a mock LiveTrade object."""
    trade = MagicMock()
    trade.id = kwargs.get("id", "test-uuid")
    trade.status = kwargs.get("status", "CLOSED")
    trade.pnl = kwargs.get("pnl", 0.0)
    trade.rr = kwargs.get("rr", 1.5)
    trade.result = kwargs.get("result", None)
    trade.broker = kwargs.get("broker", "kraken")
    trade.symbol = kwargs.get("symbol", "BTCUSDT")
    trade.side = kwargs.get("side", "long")
    trade.ts_open = kwargs.get("ts_open", datetime(2026, 2, 15, 10, 0, tzinfo=timezone.utc))
    trade.ts_close = kwargs.get("ts_close", datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc))
    return trade


class TestStatsFromDB:
    """All stats must come from live_trades table only."""

    def test_stats_from_db_only(self):
        """CLOSED trades with known PnL produce correct metrics."""
        from backend.routes.stats import summary
        from datetime import date

        trades = [
            _make_trade(pnl=10.0, result="win", rr=2.0, broker="kraken"),
            _make_trade(pnl=-5.0, result="loss", rr=1.0, broker="kraken"),
            _make_trade(pnl=8.0, result="win", rr=1.8, broker="kraken"),
        ]

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = trades
        # .count() calls: closed_unknown, total_in_db, failed_count
        mock_query.count.side_effect = [0, 3, 0]

        from contextlib import contextmanager
        @contextmanager
        def mock_session():
            yield mock_db

        with patch("backend.routes.stats.get_db_session", mock_session):
            result = summary(
                from_=date(2026, 1, 1),
                to=date(2026, 12, 31),
            )

        assert result["trades"] == 3
        assert result["pnl"] == 13.0
        assert result["wins"] == 2
        assert result["losses"] == 1
        assert result["win_rate"] > 0
        assert result["profit_factor"] > 0
        assert "by_broker" in result
        assert "kraken" in result["by_broker"]
        assert "kraken" in result["by_broker"]

    def test_failed_trades_excluded(self):
        """FAILED trades must NOT be counted in stats."""
        from backend.routes.stats import summary
        from datetime import date

        closed_trade = _make_trade(pnl=10.0, result="win", status="CLOSED")

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        # New /summary makes 4 queries:
        # 1) .all() → closed_with_pnl (only CLOSED trades with known PnL)
        mock_query.all.return_value = [closed_trade]
        # 2-4) .count() → closed_unknown, total_in_db, failed_count
        mock_query.count.side_effect = [0, 4, 3]

        from contextlib import contextmanager
        @contextmanager
        def mock_session():
            yield mock_db

        with patch("backend.routes.stats.get_db_session", mock_session):
            result = summary(
                from_=date(2026, 1, 1),
                to=date(2026, 12, 31),
            )

        # Only 1 CLOSED trade with PnL should count
        assert result["trades"] == 1
        assert result["pnl"] == 10.0
        assert result["failed_trades"] == 3  # FAILED + SKIPPED + ARCHIVED

    def test_closed_unknown_pnl_excluded(self):
        """CLOSED trades with pnl=NULL must NOT affect stats."""
        from backend.routes.stats import summary
        from datetime import date

        known_trade = _make_trade(pnl=5.0, result="win", status="CLOSED")

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        # 1) .all() → only trade with known PnL
        mock_query.all.return_value = [known_trade]
        # 2-4) .count() → closed_unknown=1, total_in_db=2, failed_count=0
        mock_query.count.side_effect = [1, 2, 0]

        from contextlib import contextmanager
        @contextmanager
        def mock_session():
            yield mock_db

        with patch("backend.routes.stats.get_db_session", mock_session):
            result = summary(
                from_=date(2026, 1, 1),
                to=date(2026, 12, 31),
            )

        assert result["trades"] == 1  # Only the one with known PnL
        assert result["closed_with_pnl"] == 1
        assert result["closed_unknown"] == 1
        assert result["pnl"] == 5.0

    def test_equity_curve_and_drawdown(self):
        """Max drawdown must reflect peak-to-trough from equity curve."""
        from backend.routes.stats import summary
        from datetime import date

        # Sequence: +10, +5, -8, +3 → peak=15, trough=7, DD=8
        trades = [
            _make_trade(pnl=10.0, result="win", ts_close=datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc)),
            _make_trade(pnl=5.0, result="win", ts_close=datetime(2026, 2, 1, 11, 0, tzinfo=timezone.utc)),
            _make_trade(pnl=-8.0, result="loss", ts_close=datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)),
            _make_trade(pnl=3.0, result="win", ts_close=datetime(2026, 2, 1, 13, 0, tzinfo=timezone.utc)),
        ]

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        # .count() calls: closed_unknown, total_in_db, failed_count
        mock_query.count.side_effect = [0, 4, 0]
        mock_query.all.return_value = trades

        from contextlib import contextmanager
        @contextmanager
        def mock_session():
            yield mock_db

        with patch("backend.routes.stats.get_db_session", mock_session):
            result = summary(
                from_=date(2026, 1, 1),
                to=date(2026, 12, 31),
            )

        assert result["pnl"] == 10.0  # 10+5-8+3
        assert result["max_dd"] == 8.0  # peak 15 - trough 7
        assert result["trades"] == 4

    def test_failed_trades_not_in_heatmap(self):
        """FAILED trades must NOT be counted in the trading heatmap."""
        from backend.routes.stats import heatmap
        
        trades = [
            _make_trade(pnl=10.0, result="win", status="CLOSED", ts_open=datetime(2026, 2, 1, 10, 0, tzinfo=timezone.utc), ts_close=None),
            _make_trade(pnl=0.0, result=None, status="FAILED", ts_open=datetime(2026, 2, 1, 10, 30, tzinfo=timezone.utc), ts_close=None),
        ]

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.all.return_value = trades

        from contextlib import contextmanager
        @contextmanager
        def mock_session():
            yield mock_db

        from datetime import date
        with patch("backend.routes.stats.get_db_session", mock_session):
            result = heatmap(
                from_=date(2026, 1, 1),
                to=date(2026, 12, 31)
            )
        heat = result
        # Find Sunday 10am (0=Sunday...6=Saturday) 1 Feb 2026 is a Sunday, so dow=0
        sun_10 = next((h for h in heat if h["dow"] == 0 and h["hour"] == 10), None)
        assert sun_10 is not None
        assert sun_10["trades"] == 1  # Only the CLOSED trade counts, not the FAILED one
        assert sun_10["pnl"] == 10.0

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
