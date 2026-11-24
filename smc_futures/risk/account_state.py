"""
AccountStateService — Broker-Aware Source of Truth for Account Equity
════════════════════════════════════════════════════════════════════
Maintains per-broker account state and exposes an *effective* state
based solely on the active execution broker.

EXECUTION_BROKER_MODE determines which broker's state gates trades:
  - KRAKEN_ONLY  → only Kraken state can block/unblock
  - BITGET_ONLY  → only Bitget state can block/unblock
  - MULTI_BROKER → both are checked independently; the *active execution
                    broker* determines the effective gate

A disabled broker NEVER pollutes the effective state.
"""

import logging
import os
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STALENESS_SEC = 120  # 2 minutes

# ── Execution Broker Mode ─────────────────────────────────────────
KRAKEN_ONLY = "KRAKEN_ONLY"
BITGET_ONLY = "BITGET_ONLY"
MULTI_BROKER = "MULTI_BROKER"

_VALID_MODES = {KRAKEN_ONLY, BITGET_ONLY, MULTI_BROKER}


def resolve_execution_broker_mode() -> str:
    """Derive execution broker mode from environment.

    Priority:
      1. EXECUTION_BROKER_MODE  (explicit)
      2. Inferred from BITGET_ENABLED / KRAKEN_ENABLED flags
    """
    explicit = os.getenv("EXECUTION_BROKER_MODE", "").upper().replace("-", "_")
    if explicit in _VALID_MODES:
        return explicit

    bitget_on = os.getenv("BITGET_ENABLED", "false").lower() in ("1", "true", "yes")
    kraken_on = os.getenv("KRAKEN_ENABLED", "false").lower() in ("1", "true", "yes")

    if kraken_on and not bitget_on:
        return KRAKEN_ONLY
    if bitget_on and not kraken_on:
        return BITGET_ONLY
    if bitget_on and kraken_on:
        return MULTI_BROKER
    # Neither enabled — default to BITGET_ONLY for legacy compat
    return BITGET_ONLY


class _BrokerState:
    """Per-broker balance state."""

    __slots__ = ("_available", "_equity", "_unrealized_pnl",
                 "_last_update_ts", "_update_count", "_max_staleness_sec")

    def __init__(self, max_staleness_sec: float):
        self._available: float = 0.0
        self._equity: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._last_update_ts: float = 0.0
        self._update_count: int = 0
        self._max_staleness_sec: float = max_staleness_sec

    def update(self, available: float, equity: float, unrealized_pnl: float = 0.0) -> None:
        self._available = available
        self._equity = equity
        self._unrealized_pnl = unrealized_pnl
        self._last_update_ts = time.time()
        self._update_count += 1

    @property
    def total_equity(self) -> float:
        return self._equity

    @property
    def available_balance(self) -> float:
        return self._available

    @property
    def unrealized_pnl(self) -> float:
        return self._unrealized_pnl

    @property
    def last_update_ts(self) -> float:
        return self._last_update_ts

    @property
    def seconds_since_update(self) -> float:
        if self._last_update_ts <= 0:
            return float("inf")
        return time.time() - self._last_update_ts

    @property
    def is_stale(self) -> bool:
        return self.seconds_since_update > self._max_staleness_sec

    @property
    def is_coherent(self) -> bool:
        if self._update_count == 0:
            return True  # not yet initialized — not incoherent, just empty
        return self._equity > 0

    @property
    def is_initialized(self) -> bool:
        return self._update_count > 0

    @property
    def block_reason(self) -> str:
        if self._update_count == 0:
            return "ACCOUNT_NOT_INITIALIZED"
        if not self.is_coherent:
            return f"INCOHERENT_EQUITY:{self._equity:.2f}"
        if self.is_stale:
            return f"STALE_BALANCE:{self.seconds_since_update:.0f}s>{self._max_staleness_sec:.0f}s"
        return ""

    def get_snapshot(self) -> dict:
        return {
            "equity": round(self._equity, 2),
            "available": round(self._available, 2),
            "unrealized_pnl": round(self._unrealized_pnl, 4),
            "stale": self.is_stale,
            "coherent": self.is_coherent,
            "age_sec": round(self.seconds_since_update, 1),
            "blocked": bool(self.block_reason),
            "block_reason": self.block_reason,
            "updates": self._update_count,
        }

    def __repr__(self) -> str:
        return (
            f"BrokerState(eq={self._equity:.2f}, avail={self._available:.2f}, "
            f"stale={self.is_stale}, coherent={self.is_coherent})"
        )


class AccountStateService:
    """Broker-aware source of truth for account equity/balance.

    Maintains separate state per broker.  The *effective* state used
    by pre-flight gates comes ONLY from the active execution broker.
    """

    def __init__(
        self,
        max_staleness_sec: float = _DEFAULT_MAX_STALENESS_SEC,
        execution_broker_mode: Optional[str] = None,
    ):
        self._max_staleness_sec = max_staleness_sec
        self._execution_broker_mode: str = (
            execution_broker_mode or resolve_execution_broker_mode()
        )
        self._brokers: Dict[str, _BrokerState] = {
            "bitget": _BrokerState(max_staleness_sec),
            "kraken": _BrokerState(max_staleness_sec),
        }
        logger.info(
            "[ACCOUNT_STATE] Initialized | mode=%s | staleness=%ss",
            self._execution_broker_mode, max_staleness_sec,
        )

    # ── Execution mode ────────────────────────────────────────────

    @property
    def execution_broker_mode(self) -> str:
        return self._execution_broker_mode

    @execution_broker_mode.setter
    def execution_broker_mode(self, mode: str) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"Invalid execution_broker_mode: {mode!r}. Must be one of {_VALID_MODES}")
        self._execution_broker_mode = mode
        logger.info("[ACCOUNT_STATE] Mode changed to %s", mode)

    @property
    def active_broker_name(self) -> str:
        """Name of the broker whose state is authoritative for gating."""
        if self._execution_broker_mode == KRAKEN_ONLY:
            return "kraken"
        if self._execution_broker_mode == BITGET_ONLY:
            return "bitget"
        # MULTI_BROKER: pick whichever is initialized; prefer default
        for name in ("kraken", "bitget"):
            if self._brokers[name].is_initialized:
                return name
        return "bitget"

    def _effective(self) -> _BrokerState:
        return self._brokers[self.active_broker_name]

    # ── Writers (broker-specific) ─────────────────────────────────

    def update(
        self,
        available: float,
        equity: float,
        unrealized_pnl: float = 0.0,
        broker: str = "bitget",
    ) -> None:
        """Push fresh balance data.  *broker* identifies the source."""
        broker = broker.lower()
        if broker not in self._brokers:
            logger.warning("[ACCOUNT_STATE] Unknown broker %r — ignored", broker)
            return

        self._brokers[broker].update(available, equity, unrealized_pnl)

        if not self._brokers[broker].is_coherent:
            logger.critical(
                "[ACCOUNT_STATE] INCOHERENT | broker=%s | equity=%.2f "
                "available=%.2f upnl=%.2f",
                broker, equity, available, unrealized_pnl,
            )
        else:
            logger.debug(
                "[ACCOUNT_STATE] Updated | broker=%s | equity=%.2f available=%.2f",
                broker, equity, available,
            )

    # ── Readers (effective = active broker only) ──────────────────

    @property
    def total_equity(self) -> float:
        return self._effective().total_equity

    @property
    def available_balance(self) -> float:
        return self._effective().available_balance

    @property
    def unrealized_pnl(self) -> float:
        return self._effective().unrealized_pnl

    @property
    def last_update_ts(self) -> float:
        return self._effective().last_update_ts

    @property
    def seconds_since_update(self) -> float:
        return self._effective().seconds_since_update

    @property
    def is_stale(self) -> bool:
        return self._effective().is_stale

    @property
    def is_coherent(self) -> bool:
        return self._effective().is_coherent

    @property
    def block_reason(self) -> str:
        return self._effective().block_reason

    # ── Per-broker access ─────────────────────────────────────────

    def broker_state(self, broker: str) -> _BrokerState:
        """Get raw state for a specific broker."""
        return self._brokers[broker.lower()]

    # ── Snapshot ──────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        eff = self._effective()
        snap = eff.get_snapshot()
        snap["execution_broker_mode"] = self._execution_broker_mode
        snap["active_broker"] = self.active_broker_name
        snap["per_broker"] = {
            name: bs.get_snapshot() for name, bs in self._brokers.items()
        }
        return snap

    # ── Legacy compat: _update_count (used by old tests) ──────────

    @property
    def _update_count(self) -> int:
        return self._effective()._update_count

    def __repr__(self) -> str:
        return (
            f"AccountStateService(mode={self._execution_broker_mode}, "
            f"active={self.active_broker_name}, "
            f"eq={self.total_equity:.2f}, coherent={self.is_coherent})"
        )
