"""
Centralized Configuration Module using Pydantic-Settings
=========================================================

This module provides type-safe, validated configuration management with:
- Automatic .env file loading
- Type validation at startup (fail-fast)
- SecretStr for credentials (prevents accidental logging)
- Validator decorators for complex business rules
- Single source of truth for all config

Installation:
    pip install pydantic-settings

Usage:
    from smc_futures.settings import settings
    
    if settings.auto_execute_enabled:
        print(f"Trading {settings.bot_symbols}")

Migration:
    Replace `SMC_CONFIG["KEY"]` with `settings.key` (snake_case)
    Replace `os.getenv("KEY")` with `settings.key`

Author: SMC Trading Bot Framework
"""

from functools import lru_cache
from typing import List, Literal, Optional, Any, Tuple, Type, Union
import logging
import sys
import json as json_module

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    from pydantic import SecretStr, field_validator, model_validator, Field
    PYDANTIC_V2 = True
except ImportError:
    try:
        from pydantic import BaseSettings, SecretStr, validator as field_validator, root_validator as model_validator, Field
        from pydantic import ConfigDict as SettingsConfigDict
        PYDANTIC_V2 = False
    except ImportError:
        print("ERROR: pydantic-settings not installed. Run: pip install pydantic-settings", file=sys.stderr)
        raise

logger = logging.getLogger(__name__)


def parse_csv_list(value: Any) -> List[str]:
    """Parse comma-separated string or JSON array into list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        # Try JSON first
        if value.startswith("["):
            try:
                result = json_module.loads(value)
                return [str(x) for x in result] if isinstance(result, list) else [str(result)]
            except json_module.JSONDecodeError:
                pass
        # Fallback to comma-separated
        return [s.strip() for s in value.split(",") if s.strip()]
    return [str(value)]


# Type alias for fields that accept both string and list from environment
StrOrList = Union[str, List[str]]


class TradingSettings(BaseSettings):
    """
    Validated trading configuration with type safety.
    
    All environment variables are automatically loaded from .env file.
    Validation happens at instantiation (startup), failing fast on misconfig.
    """
    
    # ══════════════════════════════════════════════════════════════════════════
    # ENVIRONMENT & DEPLOYMENT
    # ══════════════════════════════════════════════════════════════════════════
    
    node_env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Deployment environment. 'production' enables strict security guards."
    )
    
    dev_mode: bool = Field(
        default=False,
        description="Enable developer features (verbose logging, mock bypasses)"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # CREDENTIALS (SecretStr prevents accidental logging)
    # ══════════════════════════════════════════════════════════════════════════
    
    bitget_api_key: SecretStr = Field(
        ...,  # Required
        description="Bitget V2 Futures API Key"
    )
    bitget_api_secret: SecretStr = Field(
        ...,  # Required
        description="Bitget V2 Futures API Secret"
    )
    bitget_passphrase: SecretStr = Field(
        ...,  # Required
        description="Bitget V2 API Passphrase"
    )
    backend_api_key: Optional[SecretStr] = Field(
        default=None,
        description="Secure key for signing internal requests"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # KRAKEN FUTURES CREDENTIALS (Optional - for multi-broker support)
    # ══════════════════════════════════════════════════════════════════════════
    
    kraken_api_key: Optional[SecretStr] = Field(
        default=None,
        description="Kraken Futures API Key (public key)"
    )
    kraken_api_secret: Optional[SecretStr] = Field(
        default=None,
        description="Kraken Futures API Secret (base64 encoded)"
    )
    kraken_enabled: bool = Field(
        default=False,
        description="Enable Kraken Futures trading alongside Bitget (P0-R1 fail-closed)"
    )
    kraken_execution_enabled: bool = Field(
        default=False,
        description="Explicit enable for Kraken order execution. When false or unset, Kraken is READ_ONLY (P0-R1 fail-closed)."
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # MULTI-BROKER SETTINGS
    # ══════════════════════════════════════════════════════════════════════════
    
    default_broker: Literal["bitget", "kraken"] = Field(
        default="kraken",
        description="Default broker for order execution"
    )
    balance_check_enabled: bool = Field(
        default=True,
        description="Check account balance before placing orders"
    )
    confirm_live_multi_broker: bool = Field(
        default=False,
        description="Explicit confirmation for LIVE multi-broker trading"
    )
    mirror_trading_enabled: bool = Field(
        default=False,
        description="Enable mirror trading across multiple brokers"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # KRAKEN FUTURES SPECIFIC
    # ══════════════════════════════════════════════════════════════════════════
    
    kraken_symbols: StrOrList = Field(
        default=["BTC/USD", "ETH/USD"],
        description="Kraken Futures symbols to trade (comma-separated in .env)"
    )
    kraken_product_type: Literal["perpetual", "inverse"] = Field(
        default="perpetual",
        description="Kraken Futures product type: 'perpetual' (linear USD) or 'inverse'"
    )
    kraken_margin_mode: Literal["cross", "isolated"] = Field(
        default="cross",
        description="Kraken margin mode"
    )
    kraken_leverage: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Kraken Futures leverage multiplier"
    )
    kraken_fee_tier: float = Field(
        default=0.0002,
        ge=0.0,
        le=0.01,
        description="Kraken taker fee (0.02% = 0.0002)"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # TRADING MODE
    # ══════════════════════════════════════════════════════════════════════════
    
    trading_mode: Literal["live", "paper"] = Field(
        default="paper",
        description="Trading execution mode: 'paper' (simulated) or 'live' (real funds)"
    )
    auto_execute: Literal["true", "false", "shadow"] = Field(
        default="false",
        description="Auto-execution mode: 'true' (LIVE), 'shadow' (DRY_RUN), 'false' (MONITORING)"
    )
    force_execute: bool = Field(
        default=False,
        description="DEBUG ONLY. Bypasses soft filters (RR, session, cooldown). BLOCKED in production."
    )
    bitget_real_only: bool = Field(
        default=False,
        description="Strict mode requiring TRADING_MODE=live AND AUTO_EXECUTE=true"
    )
    allow_manual_trades: bool = Field(
        default=True,
        description="Enable manual trade execution from UI button"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # RISK MANAGEMENT
    # ══════════════════════════════════════════════════════════════════════════
    
    risk_pct: float = Field(
        default=0.01,
        ge=0.001,
        le=0.1,
        description="Risk per trade as fraction of account (0.01 = 1%)"
    )
    min_rr: float = Field(
        default=1.5,
        ge=0.5,
        le=10.0,
        description="Minimum Risk/Reward ratio allowed"
    )
    max_daily_loss_pct: float = Field(
        default=3.0,
        ge=0.0,
        le=20.0,
        description="Stop trading if daily loss exceeds this %"
    )
    max_trades_per_day: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="Maximum trades per day across all symbols"
    )
    max_open_trades_per_symbol: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Maximum open positions per symbol"
    )
    trade_cooldown_sec: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="Minimum seconds between trades on same symbol"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # SL/TP & POSITION SAFETY
    # ══════════════════════════════════════════════════════════════════════════
    
    risk_sl_pct: float = Field(
        default=1.0, ge=0.1, le=10.0,
        description="Default SL distance in %. Applied when signal has no SL."
    )
    risk_tp_pct: float = Field(
        default=2.0, ge=0.1, le=20.0,
        description="Default TP distance in %. Applied when signal has no TP."
    )
    risk_use_atr: bool = Field(
        default=False,
        description="Derive SL/TP from ATR instead of fixed %. Requires market data."
    )
    risk_atr_period: int = Field(
        default=14, ge=5, le=50,
        description="ATR period when RISK_USE_ATR=true"
    )
    risk_atr_mult_sl: float = Field(
        default=1.5, ge=0.5, le=5.0,
        description="ATR multiplier for SL distance"
    )
    risk_atr_mult_tp: float = Field(
        default=2.0, ge=0.5, le=10.0,
        description="ATR multiplier for TP distance"
    )
    risk_retry_count: int = Field(
        default=3, ge=1, le=10,
        description="Max retries for SL/TP attachment before emergency close"
    )
    risk_guard_enabled: bool = Field(
        default=True,
        description="Enable PositionSafetyGuard background loop"
    )
    risk_guard_interval_sec: int = Field(
        default=30, ge=5, le=300,
        description="PositionSafetyGuard poll interval (seconds)"
    )
    risk_max_adverse_move_pct: float = Field(
        default=1.0, ge=0.1, le=10.0,
        description="Max unrealized loss % before auto-reduce"
    )
    risk_reduce_step_pct: float = Field(
        default=25.0, ge=5.0, le=100.0,
        description="% of position to reduce on drawdown trigger"
    )
    risk_trailing_enabled: bool = Field(
        default=False,
        description="Enable trailing stop logic in DecisionEngine"
    )
    risk_trailing_activate_at_pct: float = Field(
        default=0.8, ge=0.1, le=5.0,
        description="Activate trailing when unrealized profit reaches this %"
    )
    risk_trailing_distance_pct: float = Field(
        default=0.4, ge=0.05, le=3.0,
        description="Trailing stop distance from best price in %"
    )
    decision_engine_enabled: bool = Field(
        default=True,
        description="Enable DecisionEngine autonomous position management"
    )
    decision_engine_interval_sec: int = Field(
        default=15, ge=5, le=120,
        description="DecisionEngine evaluation interval (seconds)"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # LEVERAGE & SIZING
    # ══════════════════════════════════════════════════════════════════════════
    
    bot_leverage: int = Field(
        default=3,
        ge=1,
        le=125,
        description="Default leverage (clamped to 10x in code)"
    )
    auto_risk_upscale: bool = Field(
        default=True,
        description="Auto-increase risk when raw_qty < minQty"
    )
    auto_risk_upscale_max_pct: float = Field(
        default=3.0,
        ge=0.0,
        le=10.0,
        description="Maximum risk % when auto-upscaling"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # SYMBOLS & STRATEGY
    # ══════════════════════════════════════════════════════════════════════════
    
    bot_symbols: StrOrList = Field(
        default=["BTCUSDT", "ETHUSDT"],
        description="Active trading symbols (comma-separated in .env)"
    )
    bot_strategy_mode: Literal["smc", "envelope"] = Field(
        default="smc",
        description="Strategy: 'smc' (Smart Money Concept) or 'envelope'"
    )
    bot_timeframe: str = Field(
        default="15m",
        description="Primary timeframe for analysis"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # SMC STRATEGY SPECIFIC
    # ══════════════════════════════════════════════════════════════════════════
    
    smc_htf_tf: str = Field(default="4h", description="HTF timeframe")
    smc_setup_tf: str = Field(default="15m", description="Setup timeframe")
    smc_entry_tf: str = Field(default="5m", description="Entry timeframe")
    
    smc_allow_countertrend: bool = Field(
        default=False,
        description="Allow counter-trend trades"
    )
    smc_countertrend_min_rr: float = Field(
        default=2.0,
        description="Stricter RR for counter-trend trades"
    )
    smc_neutral_permissive: bool = Field(
        default=True,
        description="Allow trading when HTF bias is neutral"
    )
    smc_order_type: Literal["market", "limit"] = Field(
        default="market",
        description="Order type for entries"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # SWEEP DETECTION
    # ══════════════════════════════════════════════════════════════════════════
    
    sweep_lookback: int = Field(default=50, ge=10, le=500)
    sweep_min_wick_atr: float = Field(default=0.3, ge=0.0, le=2.0)
    sweep_fallback_enabled: bool = Field(default=True)
    sweep_fallback_min_rr: float = Field(default=2.0, ge=1.0)
    
    # ══════════════════════════════════════════════════════════════════════════
    # ASIA STRATEGY TUNING
    # ══════════════════════════════════════════════════════════════════════════
    
    asia_range_ratio_multiplier: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="Multiplier for Asia range thresholds"
    )
    asia_allow_range_too_large: bool = Field(
        default=False,
        description="Allow entry even if range ratio exceeds threshold (requires safe conditions)"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # BACKEND
    # ══════════════════════════════════════════════════════════════════════════
    
    backend_port: int = Field(default=5000, ge=1, le=65535)
    database_url: str = Field(
        default="sqlite:///./data/trades.db",
        description="Database connection string"
    )
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info",
        description="Logging level"
    )
    
    # ══════════════════════════════════════════════════════════════════════════
    # MYSQL — Base de données marché pour backtesting
    # ══════════════════════════════════════════════════════════════════════════
    
    mysql_host: str = Field(default="localhost", description="MySQL host")
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = Field(default="root")
    mysql_password: str = Field(default="root")
    mysql_database: str = Field(default="smc_market_data")
    market_data_db_url: str = Field(
        default="",
        description="URL MySQL complète (si vide, construite depuis MYSQL_*)"
    )
    collect_symbols: str = Field(
        default="BTCUSDT,ETHUSDT,SOLUSDT",
        description="Symboles à collecter (CSV)"
    )
    collect_timeframes: str = Field(
        default="1m,5m,15m,30m,1h,4h,1d",
        description="Timeframes à collecter (CSV)"
    )
    collect_history_days: int = Field(default=90, ge=1, le=365)
    collect_live_interval_sec: int = Field(default=60, ge=10, le=3600)
    collect_broker: str = Field(default="bitget")
    
    # ══════════════════════════════════════════════════════════════════════════
    # CONFIG VALIDATION
    # ══════════════════════════════════════════════════════════════════════════
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    
    # ── List Field Validators ─────────────────────────────────────────────────
    
    @field_validator("bot_symbols", "kraken_symbols", mode="before")
    @classmethod
    def normalize_list_fields(cls, v: Any) -> List[str]:
        """
        Normalize list fields from comma-separated strings to List[str].
        Handles both 'BTCUSDT,ETHUSDT' and '["BTCUSDT","ETHUSDT"]' formats.
        """
        return parse_csv_list(v)
    
    # ── Security Validators ──────────────────────────────────────────────────
    
    @field_validator("force_execute", mode="before")
    @classmethod
    def block_force_execute_in_production(cls, v):
        """
        SECURITY: Hard-block FORCE_EXECUTE in production environment.
        Note: In pydantic v2, we can't access other fields easily in field_validator.
        Production check is done in model_validator below.
        """
        # Coerce to bool if string
        if isinstance(v, str):
            v = v.lower() in ("true", "1", "yes")
        return v

    @field_validator("kraken_enabled", "kraken_execution_enabled", mode="before")
    @classmethod
    def parse_kraken_flags_fail_closed(cls, v: Any) -> bool:
        """
        P0-R1: Fail-closed parsing for Kraken enablement flags.
        Aligned strictly with smc_config._bool(): only explicit boolean True or
        strings in ('true', '1', 'yes') evaluate to True; absent/None/empty/invalid
        values always evaluate to False.
        """
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return False
    
    @model_validator(mode='before')
    @classmethod
    def validate_security_rules(cls, data: Any) -> Any:
        """
        SECURITY: Validate security rules on the raw data.
        - Block FORCE_EXECUTE in production
        - Validate BITGET_REAL_ONLY requirements
        - Warn about ALLOW_MANUAL_TRADES in paper mode
        """
        if not isinstance(data, dict):
            return data
        
        # Get values (handle both lowercase and uppercase)
        node_env = data.get("node_env", data.get("NODE_ENV", "development"))
        force_execute = data.get("force_execute", data.get("FORCE_EXECUTE", False))
        
        # Coerce to bool if string
        if isinstance(force_execute, str):
            force_execute = force_execute.lower() in ("true", "1", "yes")
        
        # Block FORCE_EXECUTE in production
        if node_env == "production" and force_execute:
            logger.warning("[SECURITY] FORCE_EXECUTE=true blocked in production")
            data["force_execute"] = False
        
        # Validate BITGET_REAL_ONLY
        real_only = data.get("bitget_real_only", data.get("BITGET_REAL_ONLY", False))
        if isinstance(real_only, str):
            real_only = real_only.lower() in ("true", "1", "yes")
        
        trading_mode = data.get("trading_mode", data.get("TRADING_MODE", "paper"))
        auto_execute = data.get("auto_execute", data.get("AUTO_EXECUTE", "false"))
        
        if real_only:
            if trading_mode != "live":
                raise ValueError(
                    f"[REAL_ONLY] BITGET_REAL_ONLY=true requires TRADING_MODE=live "
                    f"(current: {trading_mode})"
                )
            
            if auto_execute != "true":
                raise ValueError(
                    f"[REAL_ONLY] BITGET_REAL_ONLY=true requires AUTO_EXECUTE=true "
                    f"(current: {auto_execute})"
                )

        # Multi-broker Security Guard
        kraken_enabled = data.get("kraken_enabled", data.get("KRAKEN_ENABLED", False))
        if isinstance(kraken_enabled, str):
            kraken_enabled = kraken_enabled.lower() in ("true", "1", "yes")

        mirror_enabled = data.get("mirror_trading_enabled", data.get("MIRROR_TRADING_ENABLED", False))
        if isinstance(mirror_enabled, str):
            mirror_enabled = mirror_enabled.lower() in ("true", "1", "yes")

        confirm_multi = data.get("confirm_live_multi_broker", data.get("CONFIRM_LIVE_MULTI_BROKER", False))
        if isinstance(confirm_multi, str):
            confirm_multi = confirm_multi.lower() in ("true", "1", "yes")

        if trading_mode == "live" and auto_execute == "true" and kraken_enabled and mirror_enabled:
            if not confirm_multi:
                logger.error("[SECURITY] LIVE Multi-broker mirror trading detected without confirmation!")
                raise ValueError(
                    "CRITICAL SECURITY BLOCK: LIVE multi-broker mirror trading requires "
                    "CONFIRM_LIVE_MULTI_BROKER=true in .env to proceed."
                )

        # Warn about ALLOW_MANUAL_TRADES in paper mode
        allow_manual = data.get("allow_manual_trades", data.get("ALLOW_MANUAL_TRADES", False))
        if isinstance(allow_manual, str):
            allow_manual = allow_manual.lower() in ("true", "1", "yes")
        
        trading_mode = data.get("trading_mode", data.get("TRADING_MODE", "paper"))
        
        if allow_manual and trading_mode != "live":
            logger.warning(
                "[CONFIG] ALLOW_MANUAL_TRADES=true but TRADING_MODE=paper. "
                "Manual trades will be blocked at runtime."
            )
        
        return data
    
    # ── Derived Properties ───────────────────────────────────────────────────
    
    @property
    def auto_execute_enabled(self) -> bool:
        """True if auto-execution is enabled (not shadow or false)."""
        return self.auto_execute == "true"
    
    @property
    def shadow_mode(self) -> bool:
        """True if running in shadow/dry-run mode."""
        return self.auto_execute == "shadow"
    
    @property
    def is_live_trading(self) -> bool:
        """True if live trading with auto-execution enabled."""
        return self.trading_mode == "live" and self.auto_execute_enabled
    
    @property
    def is_production(self) -> bool:
        """True if running in production environment."""
        return self.node_env == "production"
    
    def get_api_key(self) -> str:
        """Get the actual API key value (use sparingly, never log)."""
        return self.bitget_api_key.get_secret_value()
    
    def get_api_secret(self) -> str:
        """Get the actual API secret value (use sparingly, never log)."""
        return self.bitget_api_secret.get_secret_value()
    
    def get_passphrase(self) -> str:
        """Get the actual passphrase value (use sparingly, never log)."""
        return self.bitget_passphrase.get_secret_value()
    
    def get_kraken_api_key(self) -> Optional[str]:
        """Get Kraken API key if configured."""
        if self.kraken_api_key:
            return self.kraken_api_key.get_secret_value()
        return None
    
    def get_kraken_api_secret(self) -> Optional[str]:
        """Get Kraken API secret if configured."""
        if self.kraken_api_secret:
            return self.kraken_api_secret.get_secret_value()
        return None
    
    @property
    def kraken_configured(self) -> bool:
        """True if Kraken credentials are configured."""
        return bool(self.kraken_enabled and self.kraken_api_key and self.kraken_api_secret)


@lru_cache()
def get_settings() -> TradingSettings:
    """
    Get cached settings instance.
    
    Settings are loaded once and cached for performance.
    Use this function instead of instantiating TradingSettings directly.
    """
    return TradingSettings()


# Convenience singleton for direct import
# Usage: from smc_futures.settings import settings
try:
    settings = get_settings()
except Exception as e:
    logger.error(f"[SETTINGS] Failed to load configuration: {e}")
    raise


# ══════════════════════════════════════════════════════════════════════════════
# LEGACY COMPATIBILITY LAYER
# ══════════════════════════════════════════════════════════════════════════════

def to_smc_config_dict() -> dict:
    """
    Convert settings to SMC_CONFIG-compatible dict for gradual migration.
    
    Usage:
        from smc_futures.settings import to_smc_config_dict
        SMC_CONFIG = to_smc_config_dict()
    """
    s = get_settings()
    return {
        # Timeframes
        "HTF_TF": s.smc_htf_tf,
        "SETUP_TF": s.smc_setup_tf,
        "ENTRY_TF": s.smc_entry_tf,
        
        # Risk
        "MIN_RR": s.min_rr,
        "RISK_PER_TRADE_PCT": s.risk_pct,
        "DEFAULT_LEVERAGE": s.bot_leverage,
        "MAX_DAILY_LOSS_PCT": s.max_daily_loss_pct,
        "MAX_TRADES_PER_DAY": s.max_trades_per_day,
        "MAX_OPEN_TRADES_PER_SYMBOL": s.max_open_trades_per_symbol,
        "TRADE_COOLDOWN_SEC": s.trade_cooldown_sec,
        
        # Mode
        "TRADING_MODE": s.trading_mode,
        "AUTO_EXECUTE_MODE": s.auto_execute,
        "AUTO_EXECUTE_ENABLED": s.auto_execute_enabled,
        "SHADOW_MODE": s.shadow_mode,
        "FORCE_EXECUTE": s.force_execute,
        
        # Strategy
        "SYMBOLS": s.bot_symbols,
        "ALLOW_COUNTERTREND": s.smc_allow_countertrend,
        "COUNTERTREND_MIN_RR": s.smc_countertrend_min_rr,
        "NEUTRAL_PERMISSIVE": s.smc_neutral_permissive,
        "ORDER_TYPE": s.smc_order_type,
        
        # Sweep
        "SWEEP_LOOKBACK": s.sweep_lookback,
        "SWEEP_MIN_WICK_ATR": s.sweep_min_wick_atr,
        "SWEEP_FALLBACK_ENABLED": s.sweep_fallback_enabled,
        "SWEEP_FALLBACK_MIN_RR": s.sweep_fallback_min_rr,
        
        # Asia Strategy
        "ASIA_RANGE_RATIO_MULTIPLIER": s.asia_range_ratio_multiplier,
        "ASIA_ALLOW_RANGE_TOO_LARGE": s.asia_allow_range_too_large,
        
        # Product
        "BITGET_PRODUCT_TYPE": "USDT-FUTURES",
        
        # Dev / Safety
        "DEV_MODE": s.dev_mode,
        "KRAKEN_EXECUTION_ENABLED": s.kraken_execution_enabled,
    }


if __name__ == "__main__":
    # Test loading settings
    print("="*60)
    print("Settings Validation Test")
    print("="*60)
    
    try:
        s = get_settings()
        print(f"✅ Settings loaded successfully")
        print(f"   NODE_ENV:          {s.node_env}")
        print(f"   TRADING_MODE:      {s.trading_mode}")
        print(f"   AUTO_EXECUTE:      {s.auto_execute}")
        print(f"   FORCE_EXECUTE:     {s.force_execute}")
        print(f"   SYMBOLS:           {s.bot_symbols}")
        print(f"   MAX_DAILY_LOSS:    {s.max_daily_loss_pct}%")
        print(f"   API_KEY:           {s.bitget_api_key}")  # Shows SecretStr('**********')
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        sys.exit(1)
