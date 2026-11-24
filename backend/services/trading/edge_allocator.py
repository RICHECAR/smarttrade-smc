"""
Edge Allocator — Capital Allocation by Setup Edge
═════════════════════════════════════════════════

Layers above PositionSizingEngine to modulate capital allocation
based on Darwinian survival signals:

  - BOOST setups  → allocation_pct up to 1.5x, max_concurrent +1
  - KEEP  setups  → allocation_pct 1.0x (passthrough)
  - REDUCE setups → allocation_pct 0.5x, max_concurrent capped
  - BLOCK  setups → allocation_pct 0.0, max_concurrent=0

Never exceeds global safety caps:
  EDGE_MAX_TOTAL_ALLOCATION = 1.0  (sum of all live allocations)
  EDGE_MAX_SINGLE_ALLOCATION = 0.25 (max per setup)
  EDGE_MAX_CONCURRENT_GLOBAL = 8

Config via .env:
  EDGE_BOOST_MULT=1.5
  EDGE_REDUCE_MULT=0.5
  EDGE_MAX_CONCURRENT_BOOST=4
  EDGE_MAX_CONCURRENT_BASE=3
  EDGE_MAX_CONCURRENT_REDUCE=1
"""

import logging
import os
from typing import Dict, List

from backend.services.trading.darwinian_models import (
    AllocationDecision,
    SetupKey,
    SurvivalRecommendation,
    SurvivalVerdict,
)

logger = logging.getLogger("smarttrade.darwinian.allocator")


def _envf(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def _envi(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


class EdgeAllocator:
    """
    Compute capital allocation multipliers for each setup,
    based on their survival verdicts.
    """

    def __init__(self) -> None:
        self._boost_mult = _envf("EDGE_BOOST_MULT", 1.5)
        self._reduce_mult = _envf("EDGE_REDUCE_MULT", 0.5)
        self._max_conc_boost = _envi("EDGE_MAX_CONCURRENT_BOOST", 4)
        self._max_conc_base = _envi("EDGE_MAX_CONCURRENT_BASE", 3)
        self._max_conc_reduce = _envi("EDGE_MAX_CONCURRENT_REDUCE", 1)
        self._max_single = _envf("EDGE_MAX_SINGLE_ALLOCATION", 0.25)
        self._max_conc_global = _envi("EDGE_MAX_CONCURRENT_GLOBAL", 8)

    # ──────────────────────────────────────────────────
    #  PUBLIC API
    # ──────────────────────────────────────────────────

    def allocate(
        self,
        verdicts: Dict[str, SurvivalVerdict],
    ) -> Dict[str, AllocationDecision]:
        """
        Given survival verdicts for all active setups, return allocation decisions.

        Returns:
            {setup_key_str: AllocationDecision}
        """
        decisions: Dict[str, AllocationDecision] = {}
        total_concurrent = 0

        # Score-sorted: highest first for fair allocation
        sorted_keys = sorted(
            verdicts.keys(),
            key=lambda k: verdicts[k].score,
            reverse=True,
        )

        for sk in sorted_keys:
            verdict = verdicts[sk]
            rec = verdict.recommendation

            if rec == SurvivalRecommendation.BLOCK:
                decisions[sk] = AllocationDecision(
                    allocation_pct=0.0,
                    max_concurrent=0,
                    reason=f"BLOCKED (score={verdict.score:.3f})",
                )
                continue

            if rec == SurvivalRecommendation.BOOST:
                raw_pct = self._boost_mult
                max_conc = self._max_conc_boost
            elif rec == SurvivalRecommendation.KEEP:
                raw_pct = 1.0
                max_conc = self._max_conc_base
            elif rec == SurvivalRecommendation.REDUCE:
                raw_pct = self._reduce_mult
                max_conc = self._max_conc_reduce
            else:
                raw_pct = 1.0
                max_conc = self._max_conc_base

            # Score-proportional fine-tune
            pct = raw_pct * verdict.score
            pct = round(max(0.0, min(2.0, pct)), 4)  # cap multiplier at 2.0x

            # Global concurrency budget
            remaining = self._max_conc_global - total_concurrent
            effective_conc = min(max_conc, max(0, remaining))
            total_concurrent += effective_conc

            decisions[sk] = AllocationDecision(
                allocation_pct=pct,
                max_concurrent=effective_conc,
                reason=f"{rec.value} (score={verdict.score:.3f})",
            )

        logger.info(
            "[ALLOCATOR] %d setups allocated, total_concurrent=%d",
            len(decisions), total_concurrent,
        )
        return decisions

    def single_allocation(self, verdict: SurvivalVerdict) -> AllocationDecision:
        """Quick single-setup allocation (no global budget tracking)."""
        return self.allocate({"_single": verdict})["_single"]
