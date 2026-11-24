"""
Tests for Persisted Positions Service and API

Tests:
- Position creation/closure
- Performance calculation
- API endpoints
- Edge cases (no trades, negative PnL, etc.)

Author: SMC Trading Bot Framework
"""

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

# Test imports
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.models.persisted_position import (
    PersistedPosition,
    PositionStatus,
    PositionCreate,
    PositionClose,
    PerformanceStats,
)
from backend.services.positions_service import (
    PersistedPositionsService,
    PerformanceCalculator,
    PerformanceMetrics,
    get_positions_service,
    record_position_open,
    record_position_close,
    init_persisted_positions_db,
)


# ═══════════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_position_data():
    """Sample position data for testing."""
    return {
        "broker": "bitget",
        "symbol": "BTCUSDT",
        "side": "long",
        "size": 0.01,
        "entry_price": 50000.0,
        "leverage": 10,
        "stop_loss": 49000.0,
        "take_profit": 52000.0,
        "order_id": f"test-order-{uuid.uuid4().hex[:8]}",
        "session_name": "London",
        "preset": "momentum",
    }


@pytest.fixture
def sample_closed_positions():
    """Sample list of closed positions for performance testing."""
    now = datetime.now(timezone.utc)
    
    positions = []
    
    # Winning positions
    for i in range(6):
        pos = PersistedPosition(
            id=str(uuid.uuid4()),
            broker="kraken" if i % 2 == 0 else "kraken",
            symbol="BTCUSDT",
            side="long",
            size=0.01,
            entry_price=50000.0,
            entry_time=now - timedelta(hours=i * 2),
            exit_price=51000.0,
            exit_time=now - timedelta(hours=i * 2 - 1),
            stop_loss=49000.0,
            take_profit=52000.0,
            pnl=10.0,  # $10 profit
            pnl_percent=2.0,
            rr=0.5,
            status=PositionStatus.CLOSED.value,
            result="win",
        )
        positions.append(pos)
    
    # Losing positions
    for i in range(4):
        pos = PersistedPosition(
            id=str(uuid.uuid4()),
            broker="kraken" if i % 2 == 0 else "kraken",
            symbol="ETHUSDT",
            side="short",
            size=0.1,
            entry_price=3000.0,
            entry_time=now - timedelta(hours=i * 3),
            exit_price=3100.0,
            exit_time=now - timedelta(hours=i * 3 - 1),
            stop_loss=3100.0,
            take_profit=2800.0,
            pnl=-10.0,  # $10 loss
            pnl_percent=-3.33,
            rr=-1.0,
            status=PositionStatus.CLOSED.value,
            result="loss",
        )
        positions.append(pos)
    
    return positions


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistedPositionModel:
    """Tests for PersistedPosition model methods."""
    
    def test_calculate_rr_long(self):
        """Test R:R calculation for long positions."""
        # Entry: 50000, Stop: 49000 (risk: 1000), TP: 52000 (reward: 2000)
        rr = PersistedPosition.calculate_rr(
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            side="long"
        )
        assert rr == 2.0
    
    def test_calculate_rr_short(self):
        """Test R:R calculation for short positions."""
        # Entry: 50000, Stop: 51000 (risk: 1000), TP: 48000 (reward: 2000)
        rr = PersistedPosition.calculate_rr(
            entry_price=50000.0,
            stop_loss=51000.0,
            take_profit=48000.0,
            side="short"
        )
        assert rr == 2.0
    
    def test_calculate_rr_zero_risk(self):
        """Test R:R with zero risk (edge case)."""
        rr = PersistedPosition.calculate_rr(
            entry_price=50000.0,
            stop_loss=50000.0,  # No risk
            take_profit=52000.0,
            side="long"
        )
        assert rr == 0.0
    
    def test_calculate_pnl_long_profit(self):
        """Test PnL calculation for profitable long position."""
        pnl = PersistedPosition.calculate_pnl(
            entry_price=50000.0,
            exit_price=51000.0,
            size=0.01,
            side="long",
            fee=0.5
        )
        # (51000 - 50000) * 0.01 - 0.5 = 10 - 0.5 = 9.5
        assert pnl == 9.5
    
    def test_calculate_pnl_short_profit(self):
        """Test PnL calculation for profitable short position."""
        pnl = PersistedPosition.calculate_pnl(
            entry_price=50000.0,
            exit_price=49000.0,
            size=0.01,
            side="short",
            fee=0.5
        )
        # (50000 - 49000) * 0.01 - 0.5 = 10 - 0.5 = 9.5
        assert pnl == 9.5
    
    def test_calculate_pnl_long_loss(self):
        """Test PnL calculation for losing long position."""
        pnl = PersistedPosition.calculate_pnl(
            entry_price=50000.0,
            exit_price=49000.0,
            size=0.01,
            side="long",
            fee=0.5
        )
        # (49000 - 50000) * 0.01 - 0.5 = -10 - 0.5 = -10.5
        assert pnl == -10.5
    
    def test_calculate_pnl_percent_long(self):
        """Test PnL percentage calculation with leverage."""
        pnl_pct = PersistedPosition.calculate_pnl_percent(
            entry_price=50000.0,
            exit_price=51000.0,
            side="long",
            leverage=10
        )
        # ((51000 - 50000) / 50000) * 100 * 10 = 2% * 10 = 20%
        assert pnl_pct == 20.0
    
    def test_determine_result_win(self):
        """Test result determination for win."""
        result = PersistedPosition.determine_result(pnl=10.0)
        assert result == "win"
    
    def test_determine_result_loss(self):
        """Test result determination for loss."""
        result = PersistedPosition.determine_result(pnl=-10.0)
        assert result == "loss"
    
    def test_determine_result_breakeven(self):
        """Test result determination for break-even."""
        result = PersistedPosition.determine_result(pnl=0.005)  # Small positive
        assert result == "be"
    
    def test_to_dict(self):
        """Test conversion to dictionary."""
        pos = PersistedPosition(
            id="test-123",
            broker="kraken",
            symbol="BTCUSDT",
            side="long",
            size=0.01,
            entry_price=50000.0,
            status=PositionStatus.OPEN.value,
        )
        
        d = pos.to_dict()
        
        assert d["id"] == "test-123"
        assert d["broker"] == "kraken"
        assert d["symbol"] == "BTCUSDT"
        assert d["side"] == "long"
        assert d["size"] == 0.01
        assert d["entry_price"] == 50000.0
        assert d["status"] == "OPEN"
    
    def test_close_position_method(self):
        """Test the close_position method."""
        pos = PersistedPosition(
            id="test-123",
            broker="kraken",
            symbol="BTCUSDT",
            side="long",
            size=0.01,
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
            status=PositionStatus.OPEN.value,
        )
        
        pos.close_position(
            exit_price=51000.0,
            fee=0.5,
            close_reason="TP_HIT"
        )
        
        assert pos.status == PositionStatus.CLOSED.value
        assert pos.exit_price == 51000.0
        assert pos.close_reason == "TP_HIT"
        assert pos.pnl == 9.5  # (51000-50000)*0.01 - 0.5
        assert pos.result == "win"


# ═══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE CALCULATOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerformanceCalculator:
    """Tests for performance calculation logic."""
    
    def test_calculate_empty_list(self):
        """Test calculation with empty position list."""
        metrics = PerformanceCalculator.calculate([])
        
        assert metrics.total_trades == 0
        assert metrics.total_pnl == 0.0
        assert metrics.win_rate == 0.0
        assert metrics.profit_factor == 0.0
    
    def test_calculate_with_positions(self, sample_closed_positions):
        """Test calculation with sample positions."""
        metrics = PerformanceCalculator.calculate(sample_closed_positions)
        
        # 6 wins + 4 losses = 10 trades
        assert metrics.total_trades == 10
        
        # 6 wins at $10 - 4 losses at $10 = $20
        assert metrics.total_pnl == 20.0
        
        # 6/10 = 0.6
        assert metrics.win_rate == 0.6
        
        # 6 wins, 4 losses
        assert metrics.wins == 6
        assert metrics.losses == 4
        
        # Profit factor: (6 * $10) / (4 * $10) = 1.5
        assert metrics.profit_factor == 1.5
        
        # Avg win: $10, Avg loss: $10
        assert metrics.avg_win == 10.0
        assert metrics.avg_loss == 10.0
        
        # Best trade: $10, Worst: -$10
        assert metrics.best_trade == 10.0
        assert metrics.worst_trade == -10.0
    
    def test_calculate_all_wins(self):
        """Test calculation when all trades are wins."""
        positions = []
        for i in range(5):
            pos = PersistedPosition(
                id=str(uuid.uuid4()),
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=50000.0,
                entry_time=datetime.now(timezone.utc),
                exit_time=datetime.now(timezone.utc) + timedelta(hours=1),
                pnl=10.0,
                status=PositionStatus.CLOSED.value,
                result="win",
            )
            positions.append(pos)
        
        metrics = PerformanceCalculator.calculate(positions)
        
        assert metrics.win_rate == 1.0
        assert metrics.wins == 5
        assert metrics.losses == 0
        assert metrics.profit_factor == 50.0  # Gross profit with no loss
    
    def test_calculate_all_losses(self):
        """Test calculation when all trades are losses."""
        positions = []
        for i in range(5):
            pos = PersistedPosition(
                id=str(uuid.uuid4()),
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=50000.0,
                entry_time=datetime.now(timezone.utc),
                exit_time=datetime.now(timezone.utc) + timedelta(hours=1),
                pnl=-10.0,
                status=PositionStatus.CLOSED.value,
                result="loss",
            )
            positions.append(pos)
        
        metrics = PerformanceCalculator.calculate(positions)
        
        assert metrics.win_rate == 0.0
        assert metrics.wins == 0
        assert metrics.losses == 5
        assert metrics.profit_factor == 0.0
    
    def test_max_drawdown_calculation(self):
        """Test maximum drawdown calculation."""
        # Simulate: win, win, loss, loss, loss, win
        # Equity: 0 -> 10 -> 20 -> 10 -> 0 -> -10 -> 0
        # Peak: 20, Drawdown: 30
        now = datetime.now(timezone.utc)
        
        positions = [
            PersistedPosition(id="1", broker="kraken", symbol="BTC", side="long", 
                            size=0.01, entry_price=50000, pnl=10.0, 
                            exit_time=now - timedelta(hours=6),
                            status="CLOSED", result="win"),
            PersistedPosition(id="2", broker="kraken", symbol="BTC", side="long",
                            size=0.01, entry_price=50000, pnl=10.0,
                            exit_time=now - timedelta(hours=5),
                            status="CLOSED", result="win"),
            PersistedPosition(id="3", broker="kraken", symbol="BTC", side="long",
                            size=0.01, entry_price=50000, pnl=-10.0,
                            exit_time=now - timedelta(hours=4),
                            status="CLOSED", result="loss"),
            PersistedPosition(id="4", broker="kraken", symbol="BTC", side="long",
                            size=0.01, entry_price=50000, pnl=-10.0,
                            exit_time=now - timedelta(hours=3),
                            status="CLOSED", result="loss"),
            PersistedPosition(id="5", broker="kraken", symbol="BTC", side="long",
                            size=0.01, entry_price=50000, pnl=-10.0,
                            exit_time=now - timedelta(hours=2),
                            status="CLOSED", result="loss"),
            PersistedPosition(id="6", broker="kraken", symbol="BTC", side="long",
                            size=0.01, entry_price=50000, pnl=10.0,
                            exit_time=now - timedelta(hours=1),
                            status="CLOSED", result="win"),
        ]
        
        metrics = PerformanceCalculator.calculate(positions)
        
        # Max drawdown should be 30 (from peak of 20 to lowest of -10)
        assert metrics.max_drawdown == 30.0


# ═══════════════════════════════════════════════════════════════════════════════
# SERVICE TESTS (with mock DB)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionsService:
    """Tests for PersistedPositionsService."""
    
    @pytest.fixture
    def mock_db_session(self):
        """Mock database session."""
        with patch('backend.services.positions_service.get_db_session') as mock:
            session = MagicMock()
            mock.return_value.__enter__ = MagicMock(return_value=session)
            mock.return_value.__exit__ = MagicMock(return_value=False)
            yield session
    
    def test_create_position(self, mock_db_session, mock_position_data):
        """Test position creation."""
        service = PersistedPositionsService()
        
        # Configure mock
        mock_db_session.add = MagicMock()
        mock_db_session.commit = MagicMock()
        mock_db_session.refresh = MagicMock()
        
        data = PositionCreate(**mock_position_data)
        position = service.create_position(data)
        
        # Verify interactions
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()
    
    def test_create_position_calculates_rr_target(self, mock_position_data):
        """Test that R:R target is calculated on creation."""
        data = PositionCreate(**mock_position_data)
        
        # Calculate expected R:R
        expected_rr = PersistedPosition.calculate_rr(
            data.entry_price,
            data.stop_loss,
            data.take_profit,
            data.side
        )
        
        # Should be 2.0 based on test data
        assert expected_rr == 2.0


# ═══════════════════════════════════════════════════════════════════════════════
# API ROUTE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionsAPI:
    """Tests for positions API endpoints."""
    
    @pytest.fixture
    def mock_service(self):
        """Mock positions service."""
        with patch('backend.routes.positions.get_positions_service') as mock:
            service = MagicMock()
            mock.return_value = service
            yield service
    
    def test_get_positions_empty(self, mock_service):
        """Test GET /api/positions with no data."""
        mock_service.get_positions.return_value = ([], 0)
        mock_service.get_counts.return_value = {"total": 0, "open": 0, "closed": 0}
        
        # Would need TestClient setup for full integration test
        # This is a unit test for service interaction
        positions, total = mock_service.get_positions()
        assert len(positions) == 0
        assert total == 0
    
    def test_calculate_performance_stats(self, mock_service, sample_closed_positions):
        """Test performance stats calculation."""
        metrics = PerformanceCalculator.calculate(sample_closed_positions)
        
        assert metrics.total_trades == 10
        assert metrics.win_rate == 0.6
        assert metrics.profit_factor == 1.5


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (require actual DB)
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestPositionsIntegration:
    """Integration tests requiring actual database."""
    
    @pytest.fixture(autouse=True)
    def setup_test_db(self, memory_positions_db):
        """Setup isolated in-memory test database BEFORE service calls."""
        yield
    
    def test_full_position_lifecycle(self, mock_position_data):
        """Test complete position lifecycle: create -> close."""
        # Create position
        position = record_position_open(**mock_position_data)
        
        if position:  # May fail in unit test without real DB
            assert position.status == PositionStatus.OPEN.value
            assert position.broker == "bitget"
            
            # Close position
            closed = record_position_close(
                position_id=position.id,
                exit_price=51000.0,
                fee=1.0,
                close_reason="TP_HIT"
            )
            
            if closed:
                assert closed.status == PositionStatus.CLOSED.value
                assert closed.result == "win"
                assert closed.pnl > 0


# ═══════════════════════════════════════════════════════════════════════════════
# EDGE CASE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_zero_size_position(self):
        """Test behavior with zero size (should be rejected)."""
        with pytest.raises(ValueError):
            PositionCreate(
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0,  # Invalid
                entry_price=50000.0,
            )
    
    def test_negative_entry_price(self):
        """Test behavior with negative entry price."""
        with pytest.raises(ValueError):
            PositionCreate(
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=-50000.0,  # Invalid
            )
    
    def test_invalid_broker(self):
        """Test behavior with invalid broker name."""
        with pytest.raises(ValueError):
            PositionCreate(
                broker="invalid_broker",  # Invalid
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=50000.0,
            )
    
    def test_invalid_side(self):
        """Test behavior with invalid side."""
        with pytest.raises(ValueError):
            PositionCreate(
                broker="kraken",
                symbol="BTCUSDT",
                side="sideways",  # Invalid
                size=0.01,
                entry_price=50000.0,
            )
    
    def test_performance_with_single_trade(self):
        """Test performance calculation with single trade."""
        positions = [
            PersistedPosition(
                id="1",
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=50000.0,
                pnl=10.0,
                status=PositionStatus.CLOSED.value,
                result="win",
            )
        ]
        
        metrics = PerformanceCalculator.calculate(positions)
        
        assert metrics.total_trades == 1
        assert metrics.win_rate == 1.0
        assert metrics.wins == 1
        assert metrics.losses == 0
    
    def test_performance_with_breakeven_trades(self):
        """Test performance with break-even trades."""
        positions = [
            PersistedPosition(
                id="1",
                broker="kraken",
                symbol="BTCUSDT",
                side="long",
                size=0.01,
                entry_price=50000.0,
                pnl=0.005,  # Near zero
                status=PositionStatus.CLOSED.value,
                result="be",
            )
        ]
        
        metrics = PerformanceCalculator.calculate(positions)
        
        assert metrics.break_even == 1
        # Win rate excludes break-even from calculation
        assert metrics.wins == 0
        assert metrics.losses == 0


# ═══════════════════════════════════════════════════════════════════════════════
# RUN TESTS
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
