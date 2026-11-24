"""
Opportunity Capital Allocator
═════════════════════════════

Allocates capital preferentially to the highest-quality opportunities.

Allocation tiers:
  MAXIMUM   — top asymmetry (≥0.85) → 25–35% of capital
  ELEVATED  — strong edge (≥0.65)   → 15–25%
  STANDARD  — normal                → 8–15%
  REDUCED   — low edge (<0.40)      → 3–8%
  MINIMAL   — crowded/slow (<0.20)  → 0–3%

Inputs:
  • Ranked opportunities (from OpportunityRankingEngine)
  • Timing scores (from TimingPrecisionEngine)
  • Aggression multiplier (from AggressionAmplifier)

Constraints:
  • Total allocation ≤ 100% (configurable max)
  • Single symbol cap (configurable)
  • Concentration warning when top 3 > 70%

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.opportunity_dominance_models import (
    AllocationDecision,
    AllocationTier,
    CapitalPlan,
    RankedOpportunity,
)


class OpportunityCapitalAllocator:
    """Allocate capital weighted by opportunity quality."""

    # Tier → (min_pct, max_pct) of available capital
    TIER_RANGES = {
        AllocationTier.MAXIMUM:  (0.25, 0.35),
        AllocationTier.ELEVATED: (0.15, 0.25),
        AllocationTier.STANDARD: (0.08, 0.15),
        AllocationTier.REDUCED:  (0.03, 0.08),
        AllocationTier.MINIMAL:  (0.00, 0.03),
    }

    # Composite score → tier
    TIER_THRESHOLDS = [
        (0.85, AllocationTier.MAXIMUM),
        (0.65, AllocationTier.ELEVATED),
        (0.40, AllocationTier.STANDARD),
        (0.20, AllocationTier.REDUCED),
        (0.00, AllocationTier.MINIMAL),
    ]

    MAX_TOTAL_ALLOCATION = 1.00     # 100% max
    MAX_SINGLE_SYMBOL = 0.35        # 35% max per symbol
    CONCENTRATION_THRESHOLD = 0.70  # warn if top 3 > 70%

    def __init__(self) -> None:
        self._last_plan: CapitalPlan | None = None

    def allocate(
        self,
        ranked_opportunities: list[RankedOpportunity],
        *,
        timing_scores: dict[str, float] | None = None,
        aggression_multiplier: float = 1.0,
        available_capital_pct: float = 1.0,     # 0–1 of portfolio
    ) -> CapitalPlan:
        """
        Generate capital allocation plan from ranked opportunities.

        Only non-suppressed opportunities receive allocations.
        """
        timing_map = timing_scores or {}

        # Filter to non-suppressed
        active = [r for r in ranked_opportunities if not r.suppressed]

        allocations: list[AllocationDecision] = []
        total_allocated = 0.0

        for i, opp in enumerate(active):
            if total_allocated >= available_capital_pct * self.MAX_TOTAL_ALLOCATION:
                break

            tier = self._score_to_tier(opp.composite_rank_score)
            min_pct, max_pct = self.TIER_RANGES[tier]

            # Scale within tier range by composite score
            tier_range = max_pct - min_pct
            score_in_tier = min(1.0, opp.composite_rank_score)
            base_pct = min_pct + tier_range * score_in_tier

            # Boost by timing quality
            timing = timing_map.get(opp.symbol, 0.5)
            timing_boost = 1.0 + (timing - 0.5) * 0.3  # ±15%

            # Boost by aggression
            aggression_boost = min(1.5, aggression_multiplier)

            # Final allocation
            alloc_pct = base_pct * timing_boost * aggression_boost
            alloc_pct = min(alloc_pct, self.MAX_SINGLE_SYMBOL)

            # Don't exceed total maximum
            remaining = (available_capital_pct * self.MAX_TOTAL_ALLOCATION) - total_allocated
            alloc_pct = min(alloc_pct, remaining)
            alloc_pct = max(0.0, alloc_pct)

            total_allocated += alloc_pct

            # Speed score: how quickly-moving is this setup
            speed = min(1.0, opp.opportunity_score * timing)

            allocations.append(AllocationDecision(
                symbol=opp.symbol,
                tier=tier,
                capital_pct=round(alloc_pct, 4),
                priority_rank=i + 1,
                asymmetry_score=opp.opportunity_score,
                timing_score=round(timing, 4),
                speed_score=round(speed, 4),
                detail=f"tier={tier.value} base={base_pct:.3f} timing_boost={timing_boost:.2f}",
            ))

        # Concentration check
        concentrated = False
        if len(allocations) >= 3:
            top3_sum = sum(a.capital_pct for a in allocations[:3])
            concentrated = top3_sum > self.CONCENTRATION_THRESHOLD * total_allocated if total_allocated > 0 else False

        plan = CapitalPlan(
            allocations=allocations,
            total_allocated_pct=round(total_allocated, 4),
            concentrated=concentrated,
            detail=f"{len(allocations)} allocations, concentrated={concentrated}",
        )
        self._last_plan = plan
        return plan

    def get_last_plan(self) -> CapitalPlan | None:
        return self._last_plan

    def _score_to_tier(self, score: float) -> AllocationTier:
        for threshold, tier in self.TIER_THRESHOLDS:
            if score >= threshold:
                return tier
        return AllocationTier.MINIMAL
