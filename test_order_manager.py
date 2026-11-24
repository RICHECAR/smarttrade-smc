"""
Unit Tests — OrderManager Guard-Rails
Run: python -m pytest test_order_manager.py -v
"""

import time
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
import smc_futures.engine.execution.order_manager as order_manager
from smc_futures.engine.execution.order_manager import OrderManager


# ── Fixtures ──────────────────────────────────────────────

def make_config(**overrides):
    base = {
        # New config key used by OrderManager
        "AUTO_EXECUTE_ENABLED": True,
        # Legacy compatibility (some callers still use AUTO_EXECUTE)
        "AUTO_EXECUTE": True,
        "TRADING_MODE": "live",
        "MAX_TRADES_PER_DAY": 3,
        "MAX_DAILY_LOSS_PCT": 3.0,
        "TRADE_COOLDOWN_SEC": 5,
        "ORDER_TYPE": "market",
    }
    base.update(overrides)
    return base


def make_signal(direction="long", entry=65000, sl=64000, tp=67000):
    return {
        "direction": direction,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "rr_ratio": (tp - entry) / (entry - sl) if entry != sl else 0,
    }


# ── Tests ─────────────────────────────────────────────────

class TestPreFlightChecks:

    def test_auto_execute_disabled(self):
        om = OrderManager(make_config(AUTO_EXECUTE_ENABLED=False))
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig1", None, 1000, quantity=0.001)
        assert ok is False
        assert reason == "AUTO_EXECUTE_DISABLED"

    def test_kill_switch_blocks(self):
        om = OrderManager(make_config())
        om.engage_kill_switch("test_reason")
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig1", None, 1000, quantity=0.001)
        assert ok is False
        assert "KILL_SWITCH" in reason

    def test_active_trade_blocks(self):
        om = OrderManager(make_config())
        ok, reason, _ = om.pre_flight_checks(
            "BTCUSDT", make_signal(), "sig1",
            active_trade={"direction": "long"},
            account_balance=1000,
            quantity=0.001,
        )
        assert ok is False
        assert reason == "ACTIVE_TRADE"

    def test_duplicate_signal_blocks(self):
        om = OrderManager(make_config())
        # First call passes
        ok1, _, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig_dup", None, 1000, quantity=0.001)
        assert ok1 is True
        # Record the fill to mark as executed
        om.record_fill("BTCUSDT", make_signal(), {"entry_order_id": "123", "quantity": 0.001}, "sig_dup")
        # Second call with same ID blocked
        ok2, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig_dup", None, 1000, quantity=0.001)
        assert ok2 is False
        assert reason == "DUPLICATE_SIGNAL"

    def test_max_trades_per_day(self):
        om = OrderManager(make_config(MAX_TRADES_PER_DAY=2, TRADE_COOLDOWN_SEC=0))
        for i in range(2):
            ok, _, _ = om.pre_flight_checks("BTCUSDT", make_signal(), f"sig_{i}", None, 1000, quantity=0.001)
            assert ok is True
            om.record_fill("BTCUSDT", make_signal(), {"entry_order_id": str(i)}, f"sig_{i}")

        # 3rd trade should be blocked
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig_extra", None, 1000, quantity=0.001)
        assert ok is False
        assert reason == "MAX_TRADES_PER_DAY"

    def test_max_daily_loss(self):
        om = OrderManager(make_config(MAX_DAILY_LOSS_PCT=2.0))
        om._account_balance = 1000
        # Simulate a -25 USDT loss (2.5% > 2.0% limit)
        om.update_daily_pnl(-25.0)
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig1", None, 1000, quantity=0.001)
        assert ok is False
        assert "MAX_DAILY_LOSS" in reason

    def test_cooldown_active(self):
        om = OrderManager(make_config(TRADE_COOLDOWN_SEC=60))
        # Simulate recent trade
        om._last_trade_time["BTCUSDT"] = time.time()
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig1", None, 1000, quantity=0.001)
        assert ok is False
        assert reason == "COOLDOWN_ACTIVE"

    def test_zero_balance_blocks(self):
        om = OrderManager(make_config())
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig1", None, 0, quantity=0.001)
        assert ok is False
        assert reason == "ZERO_BALANCE"

    def test_full_pass(self):
        om = OrderManager(make_config(TRADE_COOLDOWN_SEC=0))
        ok, reason, _ = om.pre_flight_checks("BTCUSDT", make_signal(), "sig_ok", None, 1000, quantity=0.001)
        assert ok is True
        assert reason == "OK"


class TestKillSwitch:

    def test_engage_disengage(self):
        om = OrderManager(make_config())
        om.engage_kill_switch("manual_test")
        assert om.kill_switch_active is True
        assert om.kill_switch_reason == "manual_test"

        om.disengage_kill_switch()
        assert om.kill_switch_active is False
        assert om.kill_switch_reason == ""

    def test_auto_engage_on_max_loss(self):
        om = OrderManager(make_config(MAX_DAILY_LOSS_PCT=2.0))
        om._account_balance = 1000
        om.update_daily_pnl(-25.0)  # 2.5% loss
        assert om.kill_switch_active is True
        assert "MAX_DAILY_LOSS" in om.kill_switch_reason


class TestDailyReset:

    def test_reset_on_date_change(self):
        om = OrderManager(make_config(TRADE_COOLDOWN_SEC=0))
        # Simulate 2 trades
        om.record_fill("BTCUSDT", make_signal(), {"entry_order_id": "1"}, "s1")
        om.record_fill("BTCUSDT", make_signal(), {"entry_order_id": "2"}, "s2")
        assert om._trades_today == 2

        # Fake a date change
        om._last_reset_date = "2020-01-01"
        om.maybe_reset_daily()
        assert om._trades_today == 0
        assert om._daily_pnl == 0.0


class TestRecordFill:

    def test_fill_increments_counters(self):
        om = OrderManager(make_config(TRADE_COOLDOWN_SEC=0))
        om.record_fill("BTCUSDT", make_signal(), {"entry_order_id": "abc", "quantity": 0.001}, "sig1")
        assert om._trades_today == 1
        assert om._per_symbol_trades["BTCUSDT"] == 1
        assert "sig1" in om._executed_ids
        assert len(om._exec_log) == 1


class TestGetStats:

    def test_stats_structure(self):
        om = OrderManager(make_config())
        stats = om.get_stats()
        assert "mode" in stats
        assert "kill_switch" in stats
        assert "daily" in stats
        assert "exec_log" in stats
        assert "reject_log" in stats


class TestExecuteSignal:

    def test_successful_execution(self):
        om = OrderManager(make_config())
        mock_executor = AsyncMock()
        mock_executor.set_leverage.return_value = {"code": "00000", "leverage": 10}
        mock_executor.place_order.return_value = {
            "success": True,
            "order_id": "order_123",
            "fill_price": 65000.0,
            "quantity": 0.003,
        }

        result = asyncio.run(
            om.execute_signal(mock_executor, "BTCUSDT", make_signal(), 0.003, 10, signal_id="sig_success")
        )
        assert result["success"] is True
        mock_executor.place_order.assert_called_once()

    def test_failed_execution(self):
        om = OrderManager(make_config())
        mock_executor = AsyncMock()
        mock_executor.set_leverage.return_value = {"code": "00000", "leverage": 10}
        mock_executor.place_order.return_value = {
            "success": False,
            "error": "insufficient_balance",
        }

        result = asyncio.run(
            om.execute_signal(mock_executor, "BTCUSDT", make_signal(), 0.003, 10, signal_id="sig_failed")
        )
        assert result["success"] is False

    def test_exception_handling(self):
        om = OrderManager(make_config())
        mock_executor = AsyncMock()
        mock_executor.set_leverage.return_value = {"code": "00000", "leverage": 10}
        mock_executor.place_order.side_effect = Exception("network_error")

        result = asyncio.run(
            om.execute_signal(mock_executor, "BTCUSDT", make_signal(), 0.003, 10, signal_id="sig_exc")
        )
        assert result["success"] is False
        assert "network_error" in result["error"]

    def test_sl_attachment_failure_triggers_close(self):
        om = OrderManager(make_config())
        mock_executor = AsyncMock()
        mock_executor.set_leverage.return_value = {"code": "00000", "leverage": 10}
        mock_executor.place_order.return_value = {
            "success": True,
            "order_id": "order_123",
            "fill_price": 65000.0,
            "quantity": 0.003,
            "sl_attached": False,
        }
        mock_executor.close_position.return_value = {"success": True}

        # Ensure we do not record the fill if SL is missing
        def fail_record_fill(*args, **kwargs):
            raise AssertionError("record_fill should not be called when SL missing")
        om.record_fill = fail_record_fill

        # Ensure we don't attempt to open a trade when SL fails
        order_manager.PERSISTENCE_AVAILABLE = True
        order_manager.on_trade_opened = AsyncMock(side_effect=AssertionError("on_trade_opened should not be called"))
        order_manager.on_trade_closed = AsyncMock()

        result = asyncio.run(
            om.execute_signal(mock_executor, "BTCUSDT", make_signal(), 0.003, 10, signal_id="sig_sl_fail")
        )
        assert result["success"] is False
        assert result["error"] == "NO_SL_ATTACHED"
        mock_executor.close_position.assert_called_once()
        order_manager.on_trade_closed.assert_called_once()

        # Ensure we close the correct trade identifier (prefers order_id over signal_id)
        args, kwargs = order_manager.on_trade_closed.call_args
        assert kwargs.get("trade_id") == "order_123"
        assert kwargs.get("order_id") == "order_123"
        order_manager.on_trade_opened.assert_not_called()

    def test_multi_broker_sl_attachment_failure_triggers_rollback_and_no_persistence(self):
        om = OrderManager(make_config())
        bitget_exec = AsyncMock()
        kraken_exec = AsyncMock()
        bitget_exec.close_position = AsyncMock()

        async def fake_execute_signal(executor, symbol, signal_dict, quantity, leverage, signal_id):
            if executor is bitget_exec:
                return {
                    "success": True,
                    "entry_order_id": "order_bitget",
                    "price_executed": 65000.0,
                    "quantity": 0.003,
                    "sl_attached": True,
                }
            return {"success": False, "error": "NO_SL_ATTACHED", "status": "NO_SL_ATTACHED"}

        om.execute_signal = fake_execute_signal

        order_manager.PERSISTENCE_AVAILABLE = True
        order_manager.on_trade_opened = AsyncMock(side_effect=AssertionError("on_trade_opened should not be called"))
        order_manager.on_trade_closed = AsyncMock()

        result = asyncio.run(om.execute_multi_broker(
            symbol="BTCUSDT",
            signal_dict=make_signal(),
            signal_id="sig_multi",
            executors={"bitget": bitget_exec, "kraken": kraken_exec},
            active_trade=None,
            account_balance=1000,
            quantity=0.003,
            leverage=10,
            mode="mirror",
        ))

        assert result.success is False
        bitget_exec.close_position.assert_called_once()
        order_manager.on_trade_opened.assert_not_called()
        # No persistence should take place for the failed broker (strict mirror rollback)
        assert order_manager.on_trade_closed.call_count == 0
        assert om.kill_switch_active is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
