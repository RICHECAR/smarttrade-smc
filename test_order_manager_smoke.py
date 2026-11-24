"""Quick smoke test for OrderManager"""
import time
import asyncio
from unittest.mock import AsyncMock

from smc_futures.engine.execution.order_manager import OrderManager

def make_config(**overrides):
    base = {
        "AUTO_EXECUTE_ENABLED": True,
        "TRADING_MODE": "live",
        "MAX_TRADES_PER_DAY": 3,
        "MAX_DAILY_LOSS_PCT": 3.0,
        "TRADE_COOLDOWN_SEC": 0,
        "ORDER_TYPE": "market",
    }
    base.update(overrides)
    return base

sig = {"direction": "long", "entry": 65000, "stop_loss": 64000, "take_profit": 67000, "rr_ratio": 3.0}
passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name}")
        failed += 1

# Test 1: Auto-execute disabled
print("Test 1: Auto-execute disabled")
om = OrderManager(make_config(AUTO_EXECUTE_ENABLED=False))
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", None, 1000, 0.01)
check("Should reject", ok == False and r == "AUTO_EXECUTE_DISABLED")

# Test 2: Kill switch
print("Test 2: Kill switch")
om = OrderManager(make_config())
om.engage_kill_switch("test")
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", None, 1000, 0.01)
check("Should block", ok == False and "KILL_SWITCH" in r)
om.disengage_kill_switch()
check("Disengaged", om.kill_switch_active == False)

# Test 3: Active trade
print("Test 3: Active trade blocks")
om = OrderManager(make_config())
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", {"direction": "long"}, 1000, 0.01)
check("Should block", ok == False and r == "ACTIVE_TRADE")

# Test 4: Duplicate signal
print("Test 4: Idempotency")
om = OrderManager(make_config())
ok, _, _ = om.pre_flight_checks("BTCUSDT", sig, "dup1", None, 1000, 0.01)
check("First pass", ok == True)
om.record_fill("BTCUSDT", sig, {"entry_order_id": "x"}, "dup1")
ok, _, _ = om.pre_flight_checks("BTCUSDT", sig, "dup1", None, 1000, 0.01)
check("Duplicate blocked", ok == False)

# Test 5: Max trades/day
print("Test 5: Max trades per day")
om = OrderManager(make_config(MAX_TRADES_PER_DAY=2))
for i in range(2):
    ok, _, _ = om.pre_flight_checks("BTCUSDT", sig, f"t{i}", None, 1000, 0.01)
    om.record_fill("BTCUSDT", sig, {"entry_order_id": str(i)}, f"t{i}")
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "t_extra", None, 1000, 0.01)
check("Should block after 2", ok == False and r == "MAX_TRADES_PER_DAY")

# Test 6: Daily loss
print("Test 6: Max daily loss")
om = OrderManager(make_config(MAX_DAILY_LOSS_PCT=2.0))
om._account_balance = 1000
om.update_daily_pnl(-25.0)
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", None, 1000, 0.01)
check("Should block", ok == False and r.startswith("KILL_SWITCH"))
check("Kill switch auto-engaged", om.kill_switch_active)

# Test 7: Cooldown
print("Test 7: Cooldown")
om = OrderManager(make_config(TRADE_COOLDOWN_SEC=60))
om._last_trade_time["BTCUSDT"] = time.time()
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", None, 1000, 0.01)
check("Should block", ok == False and r == "COOLDOWN_ACTIVE")

# Test 8: Zero balance
print("Test 8: Zero balance")
om = OrderManager(make_config())
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "s1", None, 0, 0.01)
check("Should block", ok == False and r == "ZERO_BALANCE")

# Test 9: Full pass
print("Test 9: Full pass")
om = OrderManager(make_config())
ok, r, _ = om.pre_flight_checks("BTCUSDT", sig, "pass1", None, 1000, 0.01)
check("Should pass", ok == True and r == "OK")

# Test 10: Execute signal (async)
print("Test 10: Execute signal")
om = OrderManager(make_config())
mock_exec = AsyncMock()
mock_exec.set_leverage.return_value = {"code": "00000", "leverage": 10}
mock_exec.place_order.return_value = {
    "success": True,
    "order_id": "ord_123",
    "fill_price": 65000.0,
    "quantity": 0.003,
    "sl_attached": True,
}
res = asyncio.run(om.execute_signal(mock_exec, "BTCUSDT", sig, 0.003, 10))
check("Execution success", res["success"] == True)

# Test 11: Stats
print("Test 11: Stats structure")
stats = om.get_stats()
check("Has all keys", all(k in stats for k in ["mode", "kill_switch", "daily"]))

# Test 12: Daily reset
print("Test 12: Daily reset")
om = OrderManager(make_config())
om.record_fill("BTCUSDT", sig, {"entry_order_id": "1"}, "r1")
om._last_reset_date = "2020-01-01"
om.maybe_reset_daily()
check("Counters reset", om._trades_today == 0 and om._daily_pnl == 0.0)

print(f"\n{'='*40}")
print(f"Results: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
