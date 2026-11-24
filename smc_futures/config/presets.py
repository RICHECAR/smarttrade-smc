"""
SMC Strategy Presets - Dynamically Selectable Trading Configurations

Presets allow switching between different trading sessions/styles without
restarting the bot. Each preset defines a complete strategy configuration.

Usage:
    from smc_futures.config.presets import load_preset, get_active_preset_name, PRESETS
    
    # Load preset by name (merges with env overrides)
    config = load_preset("Scalp_London_Killzone")
    
    # Or load from PRESET_NAME env var
    config = load_preset()  # Uses PRESET_NAME env, defaults to Scalp_London_Killzone

Author: SMC Trading Bot Framework
"""

import os
import logging
from typing import Dict, Any, Optional, Literal
from dataclasses import dataclass, field
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ============================================================================
# PRESET DEFINITIONS
# ============================================================================

@dataclass(frozen=True)
class PresetConfig:
    """Immutable preset configuration with all strategy parameters."""
    
    name: str
    session: Literal["London", "NY"]
    description: str
    
    # Timeframe
    bot_timeframe: str = "15m"
    
    # Risk Management
    risk_pct: float = 0.01          # 1% default
    min_rr: float = 1.6
    bot_leverage: int = 10
    max_trades_per_day: int = 5
    max_daily_loss_pct: float = 3.0
    max_open_trades_per_symbol: int = 1
    trade_cooldown_sec: int = 420
    
    # Sweep Detection
    sweep_lookback: int = 20
    sweep_min_wick_atr: float = 0.12
    sweep_allow_equal_hl: bool = True
    
    # SMC Settings
    smc_neutral_permissive: bool = True
    allow_no_sweep_with_ob: bool = False
    no_sweep_min_rr: float = 1.8
    no_sweep_require_fresh_ob: bool = True
    no_sweep_block_strong_htf: bool = True
    
    # Entry Zone
    force_entry_zone: bool = False
    entry_zone_padding_atr: float = 0.12
    entry_zone_accept_nonfresh_fvg: bool = False
    entry_zone_fallback_mode: bool = True
    
    # Sweep Fallback
    sweep_fallback_enabled: bool = True
    sweep_fallback_max_fvg_age: int = 18
    fallback_allow_no_fvg: bool = False


# ============================================================================
# PRESET A: Scalp_London_Killzone (15m)
# ============================================================================
PRESET_SCALP_LONDON = PresetConfig(
    name="Scalp_London_Killzone",
    session="London",
    description="Aggressive London session scalping during killzone hours (07:00-10:00 UTC)",
    
    # Timeframe
    bot_timeframe="15m",
    
    # Risk
    risk_pct=0.01,
    min_rr=1.6,
    bot_leverage=10,
    max_trades_per_day=5,
    max_daily_loss_pct=3.0,
    max_open_trades_per_symbol=1,
    trade_cooldown_sec=420,
    
    # Sweep
    sweep_lookback=20,
    sweep_min_wick_atr=0.12,
    sweep_allow_equal_hl=True,
    
    # SMC
    smc_neutral_permissive=True,
    allow_no_sweep_with_ob=False,
    no_sweep_min_rr=1.8,
    no_sweep_require_fresh_ob=True,
    no_sweep_block_strong_htf=True,
    
    # Entry Zone
    force_entry_zone=False,
    entry_zone_padding_atr=0.12,
    entry_zone_accept_nonfresh_fvg=False,
    entry_zone_fallback_mode=True,
    
    # Fallback
    sweep_fallback_enabled=True,
    sweep_fallback_max_fvg_age=18,
    fallback_allow_no_fvg=False,
)


# ============================================================================
# PRESET B: NY_Reversal_SMC (15m)
# ============================================================================
PRESET_NY_REVERSAL = PresetConfig(
    name="NY_Reversal_SMC",
    session="NY",
    description="NY session reversal trading targeting counter-moves after London run (13:00-16:00 UTC)",
    
    # Timeframe
    bot_timeframe="15m",
    
    # Risk
    risk_pct=0.01,
    min_rr=1.8,
    bot_leverage=10,
    max_trades_per_day=4,
    max_daily_loss_pct=3.0,
    max_open_trades_per_symbol=1,
    trade_cooldown_sec=600,
    
    # Sweep (wider lookback for session highs/lows)
    sweep_lookback=60,
    sweep_min_wick_atr=0.22,
    sweep_allow_equal_hl=True,
    
    # SMC (more permissive for reversals)
    smc_neutral_permissive=True,
    allow_no_sweep_with_ob=True,
    no_sweep_min_rr=2.0,
    no_sweep_require_fresh_ob=True,
    no_sweep_block_strong_htf=True,
    
    # Entry Zone
    force_entry_zone=False,
    entry_zone_padding_atr=0.10,
    entry_zone_accept_nonfresh_fvg=True,
    entry_zone_fallback_mode=True,
    
    # Fallback
    sweep_fallback_enabled=True,
    sweep_fallback_max_fvg_age=35,
    fallback_allow_no_fvg=True,
)


# ============================================================================
# PRESET REGISTRY
# ============================================================================
PRESETS: Dict[str, PresetConfig] = {
    "Scalp_London_Killzone": PRESET_SCALP_LONDON,
    "NY_Reversal_SMC": PRESET_NY_REVERSAL,
}

DEFAULT_PRESET = "Scalp_London_Killzone"


# ============================================================================
# LOADER FUNCTIONS
# ============================================================================

def get_active_preset_name() -> str:
    """
    Get the active preset name from PRESET_NAME env var.
    Defaults to Scalp_London_Killzone if not set or invalid.
    """
    name = os.getenv("PRESET_NAME", DEFAULT_PRESET).strip()
    if name not in PRESETS:
        logger.warning(f"[PRESETS] Unknown PRESET_NAME='{name}', falling back to {DEFAULT_PRESET}")
        return DEFAULT_PRESET
    return name


def preset_to_dict(preset: PresetConfig) -> Dict[str, Any]:
    """Convert PresetConfig dataclass to flat dict for SMC_CONFIG merge."""
    return {
        "PRESET_NAME": preset.name,
        "PRESET_SESSION": preset.session,
        
        # Timeframe
        "BOT_TIMEFRAME": preset.bot_timeframe,
        "SMC_SETUP_TF": preset.bot_timeframe,  # Alias used by smc_config
        
        # Risk
        "RISK_PCT": preset.risk_pct,
        "RISK_PER_TRADE_PCT": preset.risk_pct,
        "MIN_RR": preset.min_rr,
        "SMC_MIN_RR": preset.min_rr,
        "BOT_LEVERAGE": preset.bot_leverage,
        "DEFAULT_LEVERAGE": preset.bot_leverage,
        "MAX_TRADES_PER_DAY": preset.max_trades_per_day,
        "MAX_DAILY_LOSS_PCT": preset.max_daily_loss_pct,
        "SMC_MAX_DAILY_LOSS_PCT": preset.max_daily_loss_pct,
        "MAX_OPEN_TRADES_PER_SYMBOL": preset.max_open_trades_per_symbol,
        "TRADE_COOLDOWN_SEC": preset.trade_cooldown_sec,
        
        # Sweep
        "SWEEP_LOOKBACK": preset.sweep_lookback,
        "SWEEP_MIN_WICK_ATR": preset.sweep_min_wick_atr,
        "SWEEP_ALLOW_EQUAL_HL": preset.sweep_allow_equal_hl,
        
        # SMC
        "SMC_NEUTRAL_PERMISSIVE": preset.smc_neutral_permissive,
        "NEUTRAL_PERMISSIVE": preset.smc_neutral_permissive,
        "ALLOW_NO_SWEEP_WITH_OB": preset.allow_no_sweep_with_ob,
        "NO_SWEEP_MIN_RR": preset.no_sweep_min_rr,
        "NO_SWEEP_REQUIRE_FRESH_OB": preset.no_sweep_require_fresh_ob,
        "NO_SWEEP_BLOCK_STRONG_HTF": preset.no_sweep_block_strong_htf,
        
        # Entry Zone
        "FORCE_ENTRY_ZONE": preset.force_entry_zone,
        "ENTRY_ZONE_PADDING_ATR": preset.entry_zone_padding_atr,
        "ENTRY_ZONE_ACCEPT_NONFRESH_FVG": preset.entry_zone_accept_nonfresh_fvg,
        "ENTRY_ZONE_FALLBACK_MODE": preset.entry_zone_fallback_mode,
        
        # Fallback
        "SWEEP_FALLBACK_ENABLED": preset.sweep_fallback_enabled,
        "SWEEP_FALLBACK_MAX_FVG_AGE": preset.sweep_fallback_max_fvg_age,
        "FALLBACK_ALLOW_NO_FVG": preset.fallback_allow_no_fvg,
    }


def load_preset(
    preset_name: Optional[str] = None,
    env_overrides: bool = True
) -> Dict[str, Any]:
    """
    Load a preset configuration and merge with env overrides.
    
    Args:
        preset_name: Preset name to load. If None, uses PRESET_NAME env var.
        env_overrides: If True, env vars override preset values.
    
    Returns:
        Dict with all config values (preset + env overrides).
    """
    name = preset_name or get_active_preset_name()
    preset = PRESETS.get(name, PRESETS[DEFAULT_PRESET])
    
    logger.info(f"[PRESETS] Loading preset: {preset.name} (session={preset.session})")
    
    config = preset_to_dict(preset)
    
    if env_overrides:
        # Allow env vars to override preset values
        env_mappings = {
            "RISK_PCT": ("RISK_PCT", float),
            "MIN_RR": ("MIN_RR", float),
            "BOT_LEVERAGE": ("BOT_LEVERAGE", int),
            "MAX_TRADES_PER_DAY": ("MAX_TRADES_PER_DAY", int),
            "TRADE_COOLDOWN_SEC": ("TRADE_COOLDOWN_SEC", int),
            "SWEEP_LOOKBACK": ("SWEEP_LOOKBACK", int),
        }
        
        for env_key, (config_key, converter) in env_mappings.items():
            env_val = os.getenv(env_key)
            if env_val is not None:
                try:
                    config[config_key] = converter(env_val)
                    logger.debug(f"[PRESETS] Env override: {config_key}={config[config_key]}")
                except ValueError:
                    pass
    
    return config


def get_preset_for_trade() -> Dict[str, str]:
    """
    Get minimal preset info to log with each trade.
    Returns dict with preset_name and session.
    """
    name = get_active_preset_name()
    preset = PRESETS.get(name, PRESETS[DEFAULT_PRESET])
    return {
        "preset_name": preset.name,
        "session": preset.session,
    }


def inject_preset_into_config(smc_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inject preset values into an existing SMC_CONFIG dict.
    Preset values have LOWER priority than existing smc_config (env vars win).
    
    This is useful for runtime injection without modifying smc_config.py.
    
    Args:
        smc_config: The existing SMC_CONFIG dict
    
    Returns:
        Updated config dict with preset defaults where not already set.
    """
    preset_config = load_preset(env_overrides=False)
    
    # Only inject if not already set (env takes priority)
    for key, value in preset_config.items():
        if key not in smc_config or smc_config.get(key) is None:
            smc_config[key] = value
    
    # Always inject preset metadata
    smc_config["PRESET_NAME"] = preset_config["PRESET_NAME"]
    smc_config["PRESET_SESSION"] = preset_config["PRESET_SESSION"]
    
    return smc_config


# ============================================================================
# STARTUP LOGGING
# ============================================================================
if __name__ != "__main__":
    # Auto-log preset on import
    active = get_active_preset_name()
    preset = PRESETS[active]
    logger.info(f"[PRESETS] Active: {preset.name} | Session: {preset.session} | Min RR: {preset.min_rr}")
