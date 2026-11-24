"""
Exit Reason — Normalized exit reasons for trade journal.
═════════════════════════════════════════════════════════

Every trade close MUST use one of these reasons.
Free-form strings are mapped via normalize().

Author: Chantier 5 — Trade Journal Institutionnel
"""

from enum import Enum
from typing import Optional


class ExitReason(str, Enum):
    """Canonical trade exit reasons."""

    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    BREAK_EVEN = "BREAK_EVEN"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    TIME_STOP = "TIME_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    REDUCE_ONLY_EXIT = "REDUCE_ONLY_EXIT"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"
    KILL_SWITCH_CLOSE = "KILL_SWITCH_CLOSE"
    DRAWDOWN_GOVERNOR_CLOSE = "DRAWDOWN_GOVERNOR_CLOSE"
    ACCOUNT_INCOHERENCE_CLOSE = "ACCOUNT_INCOHERENCE_CLOSE"
    PROTECTION_FAILURE_CLOSE = "PROTECTION_FAILURE_CLOSE"
    EXCHANGE_REJECT_RECOVERY_CLOSE = "EXCHANGE_REJECT_RECOVERY_CLOSE"
    LIQUIDATION = "LIQUIDATION"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def is_forced(cls, reason: "ExitReason") -> bool:
        """Return True if exit was forced by a safety system."""
        return reason in _FORCED_EXITS


_FORCED_EXITS = frozenset({
    ExitReason.EMERGENCY_CLOSE,
    ExitReason.KILL_SWITCH_CLOSE,
    ExitReason.DRAWDOWN_GOVERNOR_CLOSE,
    ExitReason.ACCOUNT_INCOHERENCE_CLOSE,
    ExitReason.PROTECTION_FAILURE_CLOSE,
    ExitReason.LIQUIDATION,
})

# ── Mapping from free-form strings to canonical reasons ──────────────────────
_ALIASES: dict[str, ExitReason] = {
    # TP variants
    "tp_hit": ExitReason.TAKE_PROFIT,
    "tp": ExitReason.TAKE_PROFIT,
    "take_profit": ExitReason.TAKE_PROFIT,
    "take-profit": ExitReason.TAKE_PROFIT,
    "takeprofit": ExitReason.TAKE_PROFIT,
    # SL variants
    "sl_hit": ExitReason.STOP_LOSS,
    "sl": ExitReason.STOP_LOSS,
    "stop_loss": ExitReason.STOP_LOSS,
    "stop-loss": ExitReason.STOP_LOSS,
    "stoploss": ExitReason.STOP_LOSS,
    # Trailing
    "trailing_stop": ExitReason.TRAILING_STOP,
    "trailing": ExitReason.TRAILING_STOP,
    "tsl": ExitReason.TRAILING_STOP,
    # Break-even
    "break_even": ExitReason.BREAK_EVEN,
    "breakeven": ExitReason.BREAK_EVEN,
    "be": ExitReason.BREAK_EVEN,
    # Signal
    "signal_reversal": ExitReason.SIGNAL_REVERSAL,
    "reversal": ExitReason.SIGNAL_REVERSAL,
    # Time
    "time_stop": ExitReason.TIME_STOP,
    "timeout": ExitReason.TIME_STOP,
    "expired": ExitReason.TIME_STOP,
    # Manual
    "manual_close": ExitReason.MANUAL_CLOSE,
    "manual": ExitReason.MANUAL_CLOSE,
    "manual_api": ExitReason.MANUAL_CLOSE,
    "user_close": ExitReason.MANUAL_CLOSE,
    # Reduce
    "reduce_only": ExitReason.REDUCE_ONLY_EXIT,
    "reduce_only_exit": ExitReason.REDUCE_ONLY_EXIT,
    # Emergency
    "emergency_close": ExitReason.EMERGENCY_CLOSE,
    "emergency": ExitReason.EMERGENCY_CLOSE,
    "emergency_sl": ExitReason.EMERGENCY_CLOSE,
    # Kill switch
    "kill_switch": ExitReason.KILL_SWITCH_CLOSE,
    "kill_switch_close": ExitReason.KILL_SWITCH_CLOSE,
    "kill_switch_l3": ExitReason.KILL_SWITCH_CLOSE,
    "l3_force_close": ExitReason.KILL_SWITCH_CLOSE,
    # Drawdown governor
    "drawdown_governor": ExitReason.DRAWDOWN_GOVERNOR_CLOSE,
    "drawdown_governor_close": ExitReason.DRAWDOWN_GOVERNOR_CLOSE,
    "governor_close": ExitReason.DRAWDOWN_GOVERNOR_CLOSE,
    # Account
    "account_incoherence": ExitReason.ACCOUNT_INCOHERENCE_CLOSE,
    "account_incoherence_close": ExitReason.ACCOUNT_INCOHERENCE_CLOSE,
    # Protection
    "protection_failure": ExitReason.PROTECTION_FAILURE_CLOSE,
    "protection_failure_close": ExitReason.PROTECTION_FAILURE_CLOSE,
    # Exchange
    "exchange_reject": ExitReason.EXCHANGE_REJECT_RECOVERY_CLOSE,
    "exchange_reject_recovery_close": ExitReason.EXCHANGE_REJECT_RECOVERY_CLOSE,
    # Liquidation
    "liquidation": ExitReason.LIQUIDATION,
    "liquidated": ExitReason.LIQUIDATION,
    # Unknown
    "unknown": ExitReason.UNKNOWN,
    "bot_close": ExitReason.UNKNOWN,
    "external_close": ExitReason.UNKNOWN,
}


def normalize(raw: Optional[str]) -> ExitReason:
    """
    Normalize a free-form exit reason string to a canonical ExitReason.

    Returns ExitReason.UNKNOWN if the string cannot be mapped.
    """
    if raw is None:
        return ExitReason.UNKNOWN
    key = raw.strip().lower()
    # Direct enum match
    for member in ExitReason:
        if key == member.value.lower():
            return member
    # Alias match
    if key in _ALIASES:
        return _ALIASES[key]
    # Prefix match for compound reasons like "L3_FORCE_CLOSE: -3.5% >= 3.0%"
    for alias, reason in _ALIASES.items():
        if key.startswith(alias):
            return reason
    return ExitReason.UNKNOWN
