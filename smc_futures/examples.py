"""
SMC Futures - Complete Usage Example
"""

import asyncio
from smc_futures import SMCFuturesOrchestrator
from smc_futures.backtest import SMCBacktester
from smc_futures.config import BITGET_CONFIG, SMC_STRATEGY_CONFIG, RISK_CONFIG
import json

# ===== EXAMPLE 1: Paper Trading with Live Data =====

async def paper_trading_example():
    """
    Simulated trading with real-time market data
    No real trades are placed, just signal generation
    """
    
    # Initialize orchestrator (paper trading mode)
    orch = SMCFuturesOrchestrator(
        symbol="BTCUSDT",
        account_balance=5000.0,
        live_trading=False  # Paper trading
    )
    
    # Simulated historical candles (replace with real data stream)
    historical_candles = [
        {
            "timestamp": 1707000000 + i*3600,  # Hourly candles
            "open": 43000 + i * 50,
            "high": 43100 + i * 50,
            "low": 42900 + i * 50,
            "close": 43050 + i * 50,
            "volume": 100
        }
        for i in range(100)
    ]
    
    print("🚀 Starting Paper Trading (SMC Futures)\n")
    
    for candle in historical_candles:
        # Process each candle
        signal = orch.process_candle(candle)
        
        # Display state every 10 candles
        if candle["timestamp"] % 10 == 0:
            state = orch.get_dashboard_state()
            print(f"\n📊 Candle: {candle['timestamp']}")
            print(f"   Price: ${candle['close']:.2f}")
            print(f"   Signals generated: {state['signal_count']}")
            
            if signal:
                print(f"   ✅ NEW SIGNAL!")
                print(f"      Direction: {signal['direction']}")
                print(f"      Entry: ${signal['entry']:.2f}")
                print(f"      SL: ${signal['stop_loss']:.2f}")
                print(f"      TP: ${signal['take_profit']:.2f}")
                print(f"      RR Ratio: {signal['rr_ratio']:.2f}x")
                print(f"      Confidence: {signal['confidence']*100:.1f}%")
    
    # Final report
    print("\n" + "="*50)
    print("📈 PAPER TRADING SUMMARY")
    print("="*50)
    final_state = orch.get_dashboard_state()
    print(f"Total signals: {final_state['signal_count']}")
    print(f"Account balance: ${final_state['account']['balance']:.2f}")
    print(f"Trades executed: {len(orch.trade_log)}")

# ===== EXAMPLE 2: Live Trading with Bitget =====

def live_trading_example():
    """
    Real trading with Bitget Futures
    ⚠️ REQUIRES VALID API CREDENTIALS ⚠️
    """
    
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    orch = SMCFuturesOrchestrator(
        symbol="BTCUSDT",
        account_balance=5000.0,
        api_key=os.getenv("BITGET_API_KEY"),
        secret_key=os.getenv("BITGET_SECRET"),
        passphrase=os.getenv("BITGET_PASSPHRASE"),
        live_trading=True  # REAL MONEY!
    )
    
    print("⚠️  LIVE TRADING MODE ENABLED")
    print("🔴 Real orders will be placed!")
    print("💰 Capital at risk!")
    
    # Connect to real market data stream
    # (replace with your data provider: Binance WS, etc)
    # 
    # for candle in market_data_stream:
    #     signal = orch.process_candle(candle)
    #     if signal:
    #         print(f"✅ Trade executed: {signal}")

# ===== EXAMPLE 3: Backtesting SMC Strategy =====

def backtest_example():
    """
    Test SMC strategy on historical data
    """
    
    # Load historical OHLCV data
    # Example: from a CSV or API call
    
    historical_data = [
        {
            "timestamp": 1700000000 + i*3600,
            "open": 40000 + (i % 100) * 100,
            "high": 40100 + (i % 100) * 100,
            "low": 39900 + (i % 100) * 100,
            "close": 40050 + (i % 100) * 100,
            "volume": 50 + (i % 50)
        }
        for i in range(500)
    ]
    
    # Initialize backtester
    backtester = SMCBacktester(
        initial_balance=10000.0,
        risk_per_trade=1.0
    )
    
    # create orchestrator for signal generation
    orch = SMCFuturesOrchestrator(
        symbol="BTCUSDT",
        account_balance=10000.0,
        live_trading=False
    )
    
    def signal_generator(candles):
        """Generate signal from candle history"""
        if not candles:
            return None
        latest = candles[-1]
        return orch.process_candle({
            "timestamp": latest.get("timestamp"),
            "open": latest.get("open"),
            "high": latest.get("high"),
            "low": latest.get("low"),
            "close": latest.get("close"),
            "volume": latest.get("volume")
        })
    
    def risk_sizing(signal, balance):
        """Size position based on risk management"""
        if not signal:
            return 0
        
        entry = signal.get("entry", 40000)
        sl = signal.get("stop_loss", entry * 0.99)
        
        risk_usd = balance * 0.01  # 1% risk
        risk_distance = abs(entry - sl)
        
        quantity = risk_usd / risk_distance if risk_distance > 0 else 0
        return quantity
    
    print("\n🧪 BACKTESTING SMC STRATEGY")
    print("="*50)
    
    # Run backtest
    metrics = backtester.run_backtest(
        candle_history=historical_data,
        signal_generator_fn=signal_generator,
        risk_engine_fn=risk_sizing
    )
    
    # Display results
    report = backtester.get_report()
    
    print("\n📊 BACKTEST RESULTS")
    print("="*50)
    print(f"\n{json.dumps(report, indent=2)}")
    
    print("\n📈 Key Metrics:")
    print(f"   Win Rate: {report['summary']['win_rate']}")
    print(f"   Profit Factor: {report['summary']['profit_factor']}")
    print(f"   Total Return: {report['returns']['return_percent']}")
    print(f"   Max Drawdown: {report['risk']['max_drawdown_percent']}")

# ===== EXAMPLE 4: Multi-Symbol Orchestration =====

async def multi_symbol_example():
    """
    Trade multiple symbols simultaneously
    """
    
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    
    # Create orchestrator for each symbol
    orchestrators = {
        symbol: SMCFuturesOrchestrator(
            symbol=symbol,
            account_balance=2000.0,  # 2000 per symbol
            live_trading=False
        )
        for symbol in symbols
    }
    
    print("🚀 Multi-Symbol SMC Trading\n")
    
    # Simulate market data for multiple symbols
    for iteration in range(50):
        print(f"\n⏱️  Iteration {iteration + 1}")
        
        for symbol in symbols:
            # Generate simulated price movement
            candle = {
                "timestamp": 1707000000 + iteration * 3600,
                "open": 40000 + iteration * 50,
                "high": 40100 + iteration * 50,
                "low": 39900 + iteration * 50,
                "close": 40050 + iteration * 50,
                "volume": 100
            }
            
            signal = orchestrators[symbol].process_candle(candle)
            
            if signal:
                print(f"   ✅ {symbol}: {signal['direction'].upper()} @ ${signal['entry']:.2f}")

# ===== MAIN: RUN EXAMPLES =====

if __name__ == "__main__":
    import sys
    
    print("""
    ╔════════════════════════════════════════════╗
    ║  🤖 SMC Futures Trading Engine - Examples  ║
    ╚════════════════════════════════════════════╝
    
    Choose an example to run:
    1. Paper Trading (simulated)
    2. Live Trading (requires API keys)
    3. Backtesting (historical data)
    4. Multi-Symbol (multiple coins)
    """)
    
    choice = input("Enter choice (1-4): ").strip()
    
    if choice == "1":
        print("\n▶️  Running Paper Trading Example...\n")
        asyncio.run(paper_trading_example())
    
    elif choice == "2":
        print("\n⚠️  WARNING: LIVE TRADING MODE!")
        print("This example REQUIRES valid Bitget API credentials.")
        confirm = input("Are you sure? (yes/no): ").strip().lower()
        if confirm == "yes":
            live_trading_example()
        else:
            print("Cancelled.")
    
    elif choice == "3":
        print("\n▶️  Running Backtest Example...\n")
        backtest_example()
    
    elif choice == "4":
        print("\n▶️  Running Multi-Symbol Example...\n")
        asyncio.run(multi_symbol_example())
    
    else:
        print("Invalid choice.")
