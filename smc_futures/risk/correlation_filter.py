"""
Correlation Filter — Prevent Correlated Multi-Asset Losses
══════════════════════════════════════════════════════════════
In crypto, BTC/ETH/SOL/XRP are highly correlated:
    BTC ↑ → ETH ↑ → SOL ↑
    BTC ↓ → ETH ↓ → SOL ↓

Without this filter, 3 SHORT positions = 3× amplified loss on a pump.

This module:
    1. Groups correlated assets
    2. Counts open positions per group
    3. Reduces risk for 2nd+ position in same group
    4. Optionally blocks excess correlated positions
"""

import logging
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Default correlation groups: majors vs alts (separate risk clusters)
_DEFAULT_GROUPS = {
    "majors": ["BTCUSDT", "ETHUSDT"],
    "alts": ["SOLUSDT", "XRPUSDT", "ADAUSDT"],
}


class CorrelationFilter:
    """
    Tracks open positions and applies risk reduction for correlated assets.

    Usage:
        cf = CorrelationFilter(config)
        mult, reason = cf.check(symbol="ETHUSDT", open_symbols={"BTCUSDT"})
        # mult=0.4, reason="CORR_REDUCTION_1_open_in_crypto_major"
    """

    def __init__(self, config: Dict):
        self.enabled: bool = config.get("CORRELATION_FILTER_ENABLED", True)
        self.risk_mult: float = config.get("CORRELATION_RISK_MULT", 0.4)
        self.max_positions: int = config.get("CORRELATION_MAX_POSITIONS", 2)

        # Parse correlation groups
        self.groups: Dict[str, List[str]] = {}
        self._symbol_to_group: Dict[str, str] = {}
        self._parse_groups(config.get("CORRELATION_GROUPS", ""))

        logger.info(
            f"[CORR_FILTER] Initialized | enabled={self.enabled}"
            f" | risk_mult={self.risk_mult} | max_pos={self.max_positions}"
            f" | groups={list(self.groups.keys())}"
        )

    def _parse_groups(self, groups_str) -> None:
        """
        Parse correlation groups from config.

        Formats supported:
            - Simple CSV: "BTCUSDT,ETHUSDT,SOLUSDT" → one group "crypto_major"
            - Multi-group: "crypto:BTCUSDT,ETHUSDT|defi:LINKUSDT,AVAXUSDT"
        """
        if isinstance(groups_str, list):
            # Already parsed (from _list helper)
            symbols = [s.strip().upper() for s in groups_str if s.strip()]
            if symbols:
                self.groups["crypto_major"] = symbols
                for s in symbols:
                    self._symbol_to_group[s] = "crypto_major"
            return

        if not groups_str or not groups_str.strip():
            # Default multi-group: majors and alts separately
            for gname, gsymbols in _DEFAULT_GROUPS.items():
                self.groups[gname] = list(gsymbols)
                for s in gsymbols:
                    self._symbol_to_group[s] = gname
            return

        if "|" in groups_str:
            # Multi-group format: "crypto:BTC,ETH|defi:LINK,AVAX"
            for part in groups_str.split("|"):
                part = part.strip()
                if ":" in part:
                    name, syms = part.split(":", 1)
                    symbols = [s.strip().upper() for s in syms.split(",") if s.strip()]
                else:
                    symbols = [s.strip().upper() for s in part.split(",") if s.strip()]
                    name = f"group_{len(self.groups)}"
                if symbols:
                    self.groups[name.strip()] = symbols
                    for s in symbols:
                        self._symbol_to_group[s] = name.strip()
        else:
            # Simple CSV: all in one group
            symbols = [s.strip().upper() for s in groups_str.split(",") if s.strip()]
            if symbols:
                self.groups["crypto_major"] = symbols
                for s in symbols:
                    self._symbol_to_group[s] = "crypto_major"

    def check(
        self,
        symbol: str,
        open_symbols: Set[str],
    ) -> Tuple[float, Optional[str]]:
        """
        Check if a new position should have reduced risk due to correlation.

        Args:
            symbol: Symbol being opened
            open_symbols: Set of symbols with currently open positions

        Returns:
            (risk_multiplier, reason_or_None)
            - (1.0, None) → no reduction
            - (0.4, "CORR_REDUCTION_...") → reduced risk
            - (0.0, "CORR_BLOCKED_...") → blocked entirely
        """
        if not self.enabled:
            return 1.0, None

        sym = symbol.upper()
        group_name = self._symbol_to_group.get(sym)

        if not group_name:
            return 1.0, None  # Symbol not in any correlation group

        group_symbols = self.groups.get(group_name, [])

        # Count open positions in same group (excluding current symbol)
        correlated_open = [
            s for s in open_symbols
            if s.upper() in group_symbols and s.upper() != sym
        ]

        count = len(correlated_open)

        if count == 0:
            return 1.0, None

        # Check max positions limit
        if count >= self.max_positions:
            reason = (
                f"CORR_BLOCKED_{count}_open_in_{group_name}"
                f"_max_{self.max_positions}"
            )
            logger.warning(
                f"[CORR_FILTER] {symbol} BLOCKED | {count} correlated open"
                f" in {group_name}: {correlated_open}"
            )
            return 0.0, reason

        # Apply risk reduction
        # Progressive: 1 open → risk_mult, 2 open → risk_mult²
        effective_mult = self.risk_mult ** count
        effective_mult = max(effective_mult, 0.1)  # Floor at 10%

        reason = (
            f"CORR_REDUCTION_{count}_open_in_{group_name}"
        )
        logger.info(
            f"[CORR_FILTER] {symbol} | risk_mult={effective_mult:.2f}"
            f" | {count} correlated open in {group_name}: {correlated_open}"
        )

        return effective_mult, reason

    def get_group_for_symbol(self, symbol: str) -> Optional[str]:
        """Get the correlation group name for a symbol."""
        return self._symbol_to_group.get(symbol.upper())

    def get_open_in_group(
        self, symbol: str, open_symbols: Set[str]
    ) -> List[str]:
        """Get list of open positions in the same correlation group."""
        sym = symbol.upper()
        group_name = self._symbol_to_group.get(sym)
        if not group_name:
            return []
        group_symbols = self.groups.get(group_name, [])
        return [s for s in open_symbols if s.upper() in group_symbols and s.upper() != sym]
