"""
SMC Futures Configuration & Setup Guide
"""

# ===== BITGET FUTURES SETUP =====

BITGET_CONFIG = {
    # API Credentials (store securely)
    "api_key": "YOUR_BITGET_API_KEY",
    "secret_key": "YOUR_BITGET_SECRET",
    "passphrase": "YOUR_BITGET_PASSPHRASE",
    
    # Trading parameters
    "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
    "account_balance": 5000.0,  # Starting balance in USDT
    "leverage": 5,  # 5x leverage (adjust to risk tolerance)
    "margin_mode": "fixed",  # or "crossed"
    "position_mode": "hedge_mode",  # or "one_way_mode"
}

# ===== SMC STRATEGY PARAMETERS =====

SMC_STRATEGY_CONFIG = {
    # Structure Detection
    "structure": {
        "lookback_period": 100,  # bars to analyze
        "min_swing_bars": 2,  # minimum bars for swing point
    },
    
    # Liquidity Detection
    "liquidity": {
        "cluster_tolerance": 0.005,  # 0.5% tolerance for equal highs/lows
        "session_based": True,  # Track session highs/lows
    },
    
    # Order Block Validation
    "order_blocks": {
        "min_displacement_bars": 2,
        "impulse_body_ratio": 0.6,  # 60% of candle must be body
        "volume_factor": 1.2,  # Volume must be 20% above average
    },
    
    # Fair Value Gap
    "fvg": {
        "min_gap_ratio": 0.0005,  # 0.05% minimum gap size
    },
    
    # Multi-Timeframe
    "mtf": {
        "htf_timeframes": ["4H", "1H"],  # Higher timeframe for bias
        "ltf_timeframes": ["15M", "5M"],  # Lower timeframe for entry
        "bias_strength_threshold": 0.5,  # 0-1 scale
    },
    
    # Signal Generation
    "signal": {
        "min_rr_ratio": 1.5,  # Minimum risk-to-reward
        "require_liquidity_trigger": True,  # Must have sweep
        "require_mtf_alignment": True,  # Must align with HTF bias
    },
}

# ===== RISK MANAGEMENT =====

RISK_CONFIG = {
    "account_balance": 5000.0,
    "risk_per_trade_percent": 1.0,  # 1% risk per trade
    "max_leverage": 10.0,  # Maximum allowed leverage
    "liquidation_buffer": 0.10,  # 10% distance to liquidation minimum
    
    # Filters
    "max_funding_rate": 0.005,  # Don't trade if funding > 0.5%
    "max_volatility_percent": 2.0,  # Don't trade if ATR > 2%
    
    # Position management
    "max_open_positions": 3,
    "position_mode": "hedge_mode",  # Can trade long and short same symbol
}

# ===== TIMEFRAME SETTINGS =====

TIMEFRAME_CONFIG = {
    # Recommended setups
    "scalping": {
        "ltf": "5M",
        "htf": "15M",
        "min_rr": 1.0,
    },
    "swing": {
        "ltf": "15M",
        "htf": "1H",
        "min_rr": 1.5,
    },
    "position": {
        "ltf": "1H",
        "htf": "4H",
        "min_rr": 2.0,
    },
}

# ===== EXAMPLE: BACKTEST CONFIGURATION =====

BACKTEST_CONFIG = {
    "symbol": "BTCUSDT",
    "timeframe": "1H",
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "initial_balance": 10000.0,
    "commission": 0.0004,  # 0.04% per side (0.0008 round trip)
    
    # Risk per trade
    "risk_per_trade": 1.0,  # percent
    
    # Use production config
    "strategy_config": SMC_STRATEGY_CONFIG,
}

# ===== EXECUTION MODES =====

MODE = {
    "BACKTEST": "backtest",  # Historical data replay
    "PAPER_TRADING": "paper",  # Simulation with live data
    "LIVE_TRADING": "live",  # Real money (CAREFUL!)
}

# Current mode
CURRENT_MODE = MODE["PAPER_TRADING"]  # Start with paper trading!

# ===== LOGGING & ALERTS =====

LOGGING_CONFIG = {
    "level": "INFO",  # DEBUG, INFO, WARNING, ERROR
    "log_file": "smc_futures.log",
    "console_output": True,
    
    # Alerts
    "alert_on_signal": True,
    "alert_on_trade": True,
    "alert_channel": "discord",  # webhook URL in .env
}
