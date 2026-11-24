"""
Strategy Allocator — Persistent learning-based allocation manager.
══════════════════════════════════════════════════════════════════

Wraps LearningEngine + persistence to:
  1. Load / refresh learning state from JSON file
  2. Expose `is_strategy_allowed(strategy, regime)` gate for order_manager
  3. Expose `get_risk_multiplier(strategy, regime)` for sizing pipeline
  4. Persist decisions to `learning_state.json`

Thread-safe: uses a lock for state mutations.

Author: Chantier 6 — Learning Loop
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from smc_futures.analytics.learning_engine import (
    AllocationDecision,
    Decision,
    LearningEngine,
    QualityScore,
)
from smc_futures.analytics.trade_journal import InstitutionalTradeJournal, TradeRecord

logger = logging.getLogger("smarttrade.analytics.allocator")

_DEFAULT_STATE_PATH = "logs/learning_state.json"
_DEFAULT_REFRESH_INTERVAL_SEC = 300  # 5 min


@dataclass
class LearningState:
    """Serializable snapshot of all allocation decisions."""
    last_refresh_ts: float = 0.0
    cycle_count: int = 0
    decisions: Dict[str, Dict] = None  # type: ignore[assignment]
    scores: Dict[str, Dict] = None  # type: ignore[assignment]
    known_strategies: List[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.decisions is None:
            self.decisions = {}
        if self.scores is None:
            self.scores = {}
        if self.known_strategies is None:
            self.known_strategies = []


class StrategyAllocator:
    """
    Learning-based strategy gating & sizing.

    Usage:
        allocator = StrategyAllocator(journal, config)
        # In order_manager pre-flight:
        if not allocator.is_strategy_allowed(strat, regime):
            return False, "STRATEGY_DISABLED_BY_LEARNING", {}
        # In sizing:
        mult = allocator.get_risk_multiplier(strat, regime)
    """

    KNOWN_STRATEGIES = [
        "DefaultSMC", "AsiaWeekend", "FundingReversion",
        "VWAPReversion", "SweepScalp", "LiqCascade", "OBImbalance",
    ]

    def __init__(
        self,
        journal: InstitutionalTradeJournal,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._journal = journal
        self._config = config or {}
        self._engine = LearningEngine(self._config)
        self._lock = threading.Lock()
        self._state = LearningState()

        # Config
        self._state_path = self._config.get(
            "LEARNING_STATE_PATH", _DEFAULT_STATE_PATH
        )
        self._refresh_interval = float(self._config.get(
            "LEARNING_REFRESH_INTERVAL_SEC", _DEFAULT_REFRESH_INTERVAL_SEC
        ))
        self._enabled = str(self._config.get(
            "LEARNING_ENGINE_ENABLED", "true"
        )).lower() in ("true", "1", "yes")

        # Load persisted state on startup
        self._load_state()

    # ── Public Gate ──────────────────────────────────────────────────────────

    def is_strategy_allowed(self, strategy: str, regime: str = "*") -> bool:
        """
        Hard gate: returns False if strategy should be completely blocked.
        Called from order_manager pre_flight_checks.
        """
        if not self._enabled:
            return True

        self._maybe_refresh()

        with self._lock:
            # Check specific (strategy, regime) first, fallback to aggregate
            dec = self._get_decision(strategy, regime)
            if dec is None:
                return True  # No data → allow

            return dec.decision != Decision.DISABLE

    def get_risk_multiplier(self, strategy: str, regime: str = "*") -> float:
        """
        Soft multiplier: adjusts risk per trade based on quality score.
        Returns 0.0 (disabled) to 1.5 (boosted).
        """
        if not self._enabled:
            return 1.0

        self._maybe_refresh()

        with self._lock:
            dec = self._get_decision(strategy, regime)
            if dec is None:
                return 1.0
            return dec.multiplier

    def get_decision(self, strategy: str, regime: str = "*") -> Optional[AllocationDecision]:
        """Get the allocation decision for a (strategy, regime) pair."""
        if not self._enabled:
            return None
        self._maybe_refresh()
        with self._lock:
            return self._get_decision(strategy, regime)

    def get_all_decisions(self) -> Dict[str, AllocationDecision]:
        """Get all allocation decisions (serialized keys)."""
        with self._lock:
            return dict(self._decisions_cache)

    def get_state_summary(self) -> Dict[str, Any]:
        """Human-readable summary for monitoring."""
        with self._lock:
            enabled_count = sum(
                1 for d in self._decisions_cache.values()
                if d.decision not in (Decision.DISABLE,)
            )
            disabled_count = sum(
                1 for d in self._decisions_cache.values()
                if d.decision == Decision.DISABLE
            )
            reduced_count = sum(
                1 for d in self._decisions_cache.values()
                if d.decision == Decision.REDUCE
            )
            return {
                "enabled": self._enabled,
                "cycle_count": self._state.cycle_count,
                "last_refresh": self._state.last_refresh_ts,
                "total_decisions": len(self._decisions_cache),
                "enabled_strategies": enabled_count,
                "disabled_strategies": disabled_count,
                "reduced_strategies": reduced_count,
            }

    def force_refresh(self) -> int:
        """Force a learning cycle. Returns number of decisions made."""
        return self._refresh()

    # ── Internal ─────────────────────────────────────────────────────────────

    @property
    def _decisions_cache(self) -> Dict[str, AllocationDecision]:
        """Flat dict {serialized_key: decision}."""
        if not hasattr(self, "_dcache"):
            self._dcache: Dict[str, AllocationDecision] = {}
        return self._dcache

    def _get_decision(self, strategy: str, regime: str) -> Optional[AllocationDecision]:
        """Look up decision: specific regime first, then aggregate."""
        key = f"{strategy}|{regime}"
        dec = self._decisions_cache.get(key)
        if dec is not None:
            return dec
        # Fallback to aggregate
        if regime != "*":
            agg_key = f"{strategy}|*"
            return self._decisions_cache.get(agg_key)
        return None

    def _maybe_refresh(self) -> None:
        """Refresh if enough time has elapsed (min 1 second between cycles)."""
        now = time.time()
        effective_interval = max(self._refresh_interval, 1.0)
        if now - self._state.last_refresh_ts >= effective_interval:
            self._refresh()

    def _refresh(self) -> int:
        """Run a full learning cycle."""
        records = self._journal.records
        if not records:
            logger.info("[LEARNING] No trade records — skipping cycle")
            return 0

        scores = self._engine.compute_scores(records)
        decisions = self._engine.make_decisions(
            scores, known_strategies=self.KNOWN_STRATEGIES,
        )

        with self._lock:
            self._dcache = {}
            for (strat, regime), dec in decisions.items():
                key = f"{strat}|{regime}"
                self._dcache[key] = dec

            self._state.cycle_count += 1
            self._state.last_refresh_ts = time.time()
            self._state.known_strategies = list(self.KNOWN_STRATEGIES)
            self._state.decisions = {
                k: {
                    "strategy": v.strategy,
                    "regime": v.regime,
                    "decision": v.decision,
                    "multiplier": v.multiplier,
                    "quality_score": v.quality_score,
                    "total_trades": v.total_trades,
                    "reason": v.reason,
                    "cooldown_remaining": v.cooldown_remaining,
                }
                for k, v in self._dcache.items()
            }
            self._state.scores = {
                f"{strat}|{regime}": {
                    "score": qs.score,
                    "total_trades": qs.total_trades,
                    "gross_pnl": qs.gross_pnl,
                    "expectancy": qs.expectancy,
                    "win_rate": qs.win_rate,
                }
                for (strat, regime), qs in scores.items()
            }

        self._save_state()

        n_disabled = sum(1 for d in decisions.values() if d.decision == Decision.DISABLE)
        n_reduced = sum(1 for d in decisions.values() if d.decision == Decision.REDUCE)
        n_boost = sum(1 for d in decisions.values() if d.decision == Decision.BOOST)
        logger.info(
            "[LEARNING] Cycle #%d — %d decisions: %d DISABLE, %d REDUCE, %d BOOST | %d records",
            self._state.cycle_count, len(decisions), n_disabled, n_reduced, n_boost, len(records),
        )

        return len(decisions)

    def _save_state(self) -> None:
        """Persist learning state to JSON."""
        try:
            path = Path(self._state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_refresh_ts": self._state.last_refresh_ts,
                "cycle_count": self._state.cycle_count,
                "decisions": self._state.decisions,
                "scores": self._state.scores,
                "known_strategies": self._state.known_strategies,
            }
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, str(path))
            logger.debug("[LEARNING] State saved to %s", path)
        except Exception:
            logger.exception("[LEARNING] Failed to save state")

    def _load_state(self) -> None:
        """Restore learning state from JSON."""
        path = Path(self._state_path)
        if not path.exists():
            logger.info("[LEARNING] No persisted state at %s — starting fresh", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._state.last_refresh_ts = data.get("last_refresh_ts", 0.0)
            self._state.cycle_count = data.get("cycle_count", 0)
            self._state.known_strategies = data.get("known_strategies", [])
            self._state.decisions = data.get("decisions", {})
            self._state.scores = data.get("scores", {})

            # Rebuild decisions cache from persisted state
            self._dcache = {}
            for key, d in self._state.decisions.items():
                self._dcache[key] = AllocationDecision(
                    strategy=d["strategy"],
                    regime=d["regime"],
                    decision=d["decision"],
                    multiplier=d["multiplier"],
                    quality_score=d["quality_score"],
                    total_trades=d["total_trades"],
                    reason=d["reason"],
                    cooldown_remaining=d.get("cooldown_remaining", 0),
                )

            logger.info(
                "[LEARNING] Loaded state: cycle=%d, %d decisions",
                self._state.cycle_count, len(self._dcache),
            )
        except Exception:
            logger.exception("[LEARNING] Failed to load state — starting fresh")
            self._state = LearningState()
