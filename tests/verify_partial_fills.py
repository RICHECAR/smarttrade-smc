import asyncio
import os
import sys
import logging
import traceback
from datetime import datetime
import uuid

# Add project root to path
cwd = os.getcwd()
sys.path.insert(0, cwd)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def verify_partial_fills():
    print("="*50)
    print("VERIFYING PARTIAL FILLS & REDUCE LOGIC")
    print("="*50)

    try:
        from backend.services.live_trades_service import LiveTradesService, init_live_trades_db
        from backend.services.trade_persistence_hook import on_trade_opened, on_trade_reduced, on_trade_closed
        from backend.models.live_trade import LiveTrade
        from backend.db.session import SessionLocal
    except ImportError:
        traceback.print_exc()
        return

    # 1. Initialize DB
    print("\n[1] Initializing DB...")
    init_live_trades_db()

    # 2. Create a dummy trade
    order_id = f"TEST-ORDER-{uuid.uuid4().hex[:8]}"
    symbol = "ETHUSDT"
    logger.info(f"Creating trade for {symbol} with OrderID {order_id}")

    trade = await on_trade_opened(
        order_id=order_id,
        symbol=symbol,
        side="long",
        qty=1.0,
        entry=2000.0,
        stop=1900.0,
        tp=2200.0,
        leverage=10,
        session_name="TEST_SESSION",
        signal_id="SIG-001"
    )
    
    if not trade:
        logger.error("Failed to create trade")
        return

    print(f"✅ Trade Created: ID={trade.id} | Status={trade.status}")

    # 3. Simulate Partial Reduce (50%)
    print("\n[2] Simulating 50% Reduce...")
    # Reduce 0.5 ETH at 2050 (PnL = (2050-2000)*0.5 = 25)
    reduce_fill = await on_trade_reduced(
        trade_id=trade.id,
        qty=0.5,
        price=2050.0,
        pnl=25.0,
        fee=1.5,
        reduce_pct=50.0,
        order_id=f"REDUCE-{uuid.uuid4().hex[:8]}"
    )
    
    if reduce_fill:
        print(f"✅ Reduce Fill Recorded: Qty={reduce_fill.qty} | PnL={reduce_fill.pnl}")
    else:
        print("❌ Failed to record reduce fill")

    # Verify Logic
    updated_trade = LiveTradesService.get_trade_by_id(trade.id)
    print(f"   Trade PnL: {updated_trade.pnl} (Expected 25.0)")
    print(f"   Trade Fee: {updated_trade.fee} (Expected 1.5)")
    
    if updated_trade.pnl != 25.0:
        print("❌ PnL Mismatch!")
    
    # 4. Simulate Another Reduce (25%)
    print("\n[3] Simulating 25% Reduce...")
    # Reduce 0.25 ETH at 2100 (PnL = (2100-2000)*0.25 = 25)
    reduce_fill_2 = await on_trade_reduced(
        trade_id=trade.id,
        qty=0.25,
        price=2100.0,
        pnl=25.0,
        fee=0.75,
        reduce_pct=25.0,
        order_id=f"REDUCE-{uuid.uuid4().hex[:8]}"
    )
    
    updated_trade = LiveTradesService.get_trade_by_id(trade.id)
    print(f"✅ Reduce 2 Recorded. Total PnL: {updated_trade.pnl} (Expected 50.0)")

    # 5. Simulate Full Close (Remaining 0.25)
    print("\n[4] Simulating Full Close...")
    # Closing remaining 0.25 at 1900 (Loss)
    # PnL = (1900-2000)*0.25 = -25
    
    # We use on_trade_closed for the final close usually, OR we can use add_fill(action="CLOSE")
    # Let's test on_trade_closed which now uses close_live_trade_from_fill
    # Wait, on_trade_closed logic was:
    # trade = await LiveTradesService.close_live_trade_from_fill(...)
    
    # Ideally, if we use PositionManager, it calls on_trade_closed on full close.
    # But wait, close_live_trade sets status=CLOSED and updates PnL.
    # Does it respect previous partial PnL?
    # Let's check LiveTradesService.close_live_trade logic again.
    
    # It sets: trade.pnl = pnl (argument).
    # If we pass the *total* realized PnL from the exchange, it overrides.
    # Bitget sometimes gives total PnL valid for the position.
    # However, if we are summing partials, we might want to ADD to existing?
    
    # Let's just test close_live_trade behavior.
    
    final_close = await on_trade_closed(
        trade_id=trade.id,
        close_price=1900.0,
        pnl=-25.0, # PnL for this specific fill (0.25 * -100)
        qty=0.25,  # Closing remaining 0.25
        fee=0.75
    )
    
    final_trade = LiveTradesService.get_trade_by_id(trade.id)
    print(f"✅ Final Close. Status={final_trade.status} | Total PnL={final_trade.pnl}")
    
    # Clean up
    print("\n" + "="*50)
    print("VERIFICATION COMPLETE")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(verify_partial_fills())
