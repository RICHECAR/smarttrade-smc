"""
Trading Performance Layer — Institutional-Grade Active Trading Engine
═════════════════════════════════════════════════════════════════════

Modules:
  - models:              Shared data models (signals, decisions, metrics)
  - decision_engine:     Entry signal generation (momentum, volatility, structure)
  - position_sizing:     Confidence-based dynamic sizing
  - risk_engine:         Kill switch, drawdown governor, consecutive loss pause
  - execution_engine:    Partial TP, scaling in/out, adaptive stop loss
  - performance_tracker: Win rate, Sharpe, drawdown, profit factor
  - trading_loop:        Async feedback loop orchestrator

CONSTRAINT: These modules NEVER modify existing services (PnL, Price, Position).
            They ONLY consume existing interfaces and extend the system.
"""
