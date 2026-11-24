"""
Opportunity Universe Filter
════════════════════════════

Dynamically blocks or allows symbols / setup families / regimes / hours
based on rolling portfolio evidence and the current system mode.

Blocking triggers:
  • Symbol: 3+ consecutive losses on symbol → block 24 h
  • Setup family: win-rate < 30% over last 20 trades → block
  • Regime mismatch: setup not validated for current regime → block
  • Hours: losing hours (configurable) → block
  • Manual block list: operator-specified symbols

Mode interaction:
  • EXPANSION  — only hard blocks apply (manual + 5+ consecutive losses)
  • OFFENSIVE  — standard blocking
  • SELECTIVE  — stricter: 2+ consecutive losses trigger block
  • DEFENSIVE  — very strict + hour blocks enabled
  • SURVIVAL   — block everything except top-3 proven setups

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from backend.services.trading.meta_control_models import (
    FilterVerdict,
    SystemMode,
    UniverseState,
)


@dataclass(frozen=True)
class _Block:
    reason: str
    detail: str
    expires_ts: float  # 0 = permanent until cleared


class OpportunityUniverseFilter:
    """Filter the opportunity universe based on mode + evidence."""

    # Consecutive-loss thresholds per mode
    _CONSEC_LOSS_THRESHOLD = {
        SystemMode.EXPANSION: 5,
        SystemMode.OFFENSIVE: 3,
        SystemMode.SELECTIVE: 2,
        SystemMode.DEFENSIVE: 2,
        SystemMode.SURVIVAL: 1,
    }

    # Win-rate threshold for family blocking
    _FAMILY_WR_THRESHOLD = 0.30
    _FAMILY_MIN_TRADES = 20

    # Block duration (seconds)
    _BLOCK_DURATION_H = 24 * 3600  # 24 hours

    def __init__(self) -> None:
        self._blocks: dict[str, _Block] = {}          # key → block
        self._manual_blocks: set[str] = set()          # manual symbol blocks
        self._symbol_losses: dict[str, int] = {}       # consecutive losses
        self._family_records: dict[str, list[bool]] = {}  # family → [win/loss]
        self._blocked_hours: set[int] = set()          # UTC hours

    # ────────────────────────────────────────────────────
    #  Configuration
    # ────────────────────────────────────────────────────

    def set_blocked_hours(self, hours: list[int]) -> None:
        self._blocked_hours = {h % 24 for h in hours}

    def add_manual_block(self, symbol: str) -> None:
        self._manual_blocks.add(symbol)

    def remove_manual_block(self, symbol: str) -> None:
        self._manual_blocks.discard(symbol)

    # ────────────────────────────────────────────────────
    #  Evidence recording
    # ────────────────────────────────────────────────────

    def record_outcome(
        self, symbol: str, family: str, won: bool
    ) -> None:
        """Record a trade outcome to update blocking evidence."""
        # Consecutive losses per symbol
        if won:
            self._symbol_losses[symbol] = 0
        else:
            self._symbol_losses[symbol] = self._symbol_losses.get(symbol, 0) + 1

        # Family win/loss tape
        tape = self._family_records.setdefault(family, [])
        tape.append(won)
        # Keep last 50
        if len(tape) > 50:
            self._family_records[family] = tape[-50:]

    # ────────────────────────────────────────────────────
    #  Filtering
    # ────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        family: str,
        regime: str,
        validated_regimes: list[str],
        mode: SystemMode,
        *,
        current_hour_utc: int | None = None,
        setup_key: str = "",
    ) -> FilterVerdict:
        """
        Check if an opportunity passes the universe filter.

        Returns FilterVerdict with allowed=True or blocked + reason.
        """
        now = time.time()
        self._purge_expired(now)

        reasons: list[str] = []

        # 1) Manual block
        if symbol in self._manual_blocks:
            reasons.append(f"manual_block:{symbol}")

        # 2) Consecutive losses
        consec = self._symbol_losses.get(symbol, 0)
        threshold = self._CONSEC_LOSS_THRESHOLD[mode]
        if consec >= threshold:
            reasons.append(
                f"consec_losses:{symbol}={consec}>={threshold}"
            )
            self._blocks[f"sym:{symbol}"] = _Block(
                reason="consecutive_losses",
                detail=f"{consec} consecutive losses",
                expires_ts=now + self._BLOCK_DURATION_H,
            )

        # 3) Active block on symbol
        sym_key = f"sym:{symbol}"
        if sym_key in self._blocks:
            reasons.append(f"active_block:{symbol}")

        # 4) Family win-rate
        tape = self._family_records.get(family, [])
        if len(tape) >= self._FAMILY_MIN_TRADES:
            wr = sum(tape) / len(tape)
            if wr < self._FAMILY_WR_THRESHOLD:
                reasons.append(f"family_wr:{family}={wr:.0%}<{self._FAMILY_WR_THRESHOLD:.0%}")

        # 5) Regime mismatch (not in EXPANSION — EXPANSION ignores this)
        if mode != SystemMode.EXPANSION:
            if regime not in validated_regimes:
                reasons.append(f"regime_mismatch:{regime} not in {validated_regimes}")

        # 6) Hour block (only DEFENSIVE / SURVIVAL)
        if mode in (SystemMode.DEFENSIVE, SystemMode.SURVIVAL):
            hour = current_hour_utc if current_hour_utc is not None else 0
            if hour in self._blocked_hours:
                reasons.append(f"blocked_hour:{hour}")

        # 7) SURVIVAL mode: only allow if symbol has recent positive record
        if mode == SystemMode.SURVIVAL:
            sym_tape = self._family_records.get(symbol, [])
            if len(sym_tape) < 5 or sum(sym_tape[-5:]) < 3:
                reasons.append("survival_filter:insufficient_proven_record")

        allowed = len(reasons) == 0
        block_reason = ""
        if not allowed:
            if any("manual_block" in r for r in reasons):
                block_reason = "manual_block"
            elif any("regime_mismatch" in r for r in reasons):
                block_reason = "bad_regime"
            elif any("family_wr" in r for r in reasons):
                block_reason = "low_edge"
            elif any("blocked_hour" in r for r in reasons):
                block_reason = "session_banned"
            elif any("survival_filter" in r for r in reasons):
                block_reason = "recovery_restriction"
            elif any("consec_losses" in r or "active_block" in r for r in reasons):
                block_reason = "poor_execution"

        return FilterVerdict(
            setup_key=setup_key or f"{symbol}_{family}",
            allowed=allowed,
            block_reason=block_reason,
        )

    def get_universe_state(self, mode: SystemMode) -> UniverseState:
        """Return current state of universe filtering."""
        now = time.time()
        self._purge_expired(now)
        blocked_syms = set(
            k.split(":", 1)[1]
            for k in self._blocks
            if k.startswith("sym:")
        ) | self._manual_blocks
        blocked_fams = set(
            fam
            for fam, tape in self._family_records.items()
            if (
                len(tape) >= self._FAMILY_MIN_TRADES
                and sum(tape) / len(tape) < self._FAMILY_WR_THRESHOLD
            )
        )
        return UniverseState(
            blocked_symbols=blocked_syms,
            blocked_families=blocked_fams,
            blocked_hours=self._blocked_hours,
            blocked_count=len(blocked_syms) + len(blocked_fams),
        )

    # ────────────────────────────────────────────────────
    #  Internal
    # ────────────────────────────────────────────────────

    def _purge_expired(self, now: float) -> None:
        expired = [
            k for k, b in self._blocks.items()
            if 0 < b.expires_ts <= now
        ]
        for k in expired:
            del self._blocks[k]
