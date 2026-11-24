"""
Strategic Rotation Engine
═════════════════════════

Re-ranks available opportunities by a mode-aware composite score:

  composite = w_edge * adjusted_edge
            + w_robust * robustness
            + w_divers * diversification_value
            + w_capeff * capital_efficiency
            + w_regime * regime_fit

Weight profiles shift with SystemMode:
  • EXPANSION  → heavier on diversification + opportunity breadth
  • OFFENSIVE  → heavier on adjusted_edge + regime_fit
  • SELECTIVE  → balanced
  • DEFENSIVE  → heavier on robustness + capital_efficiency
  • SURVIVAL   → maximum robustness, minimum risk

Outputs a ranked list of RotationDecision objects with final scores
and recommended allocation adjustments.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.meta_control_models import (
    RotationCandidate,
    RotationDecision,
    SystemMode,
)


# ── Weight profiles per mode ────────────────────────────────────────
# (edge, robustness, diversification, capital_efficiency, regime_fit)
_WEIGHT_PROFILES: dict[SystemMode, tuple[float, float, float, float, float]] = {
    SystemMode.EXPANSION:  (0.20, 0.15, 0.30, 0.15, 0.20),
    SystemMode.OFFENSIVE:  (0.35, 0.15, 0.15, 0.10, 0.25),
    SystemMode.SELECTIVE:  (0.25, 0.25, 0.15, 0.15, 0.20),
    SystemMode.DEFENSIVE:  (0.10, 0.35, 0.10, 0.30, 0.15),
    SystemMode.SURVIVAL:   (0.05, 0.50, 0.05, 0.30, 0.10),
}


class StrategicRotationEngine:
    """Rank and rotate opportunities based on mode-aware composite score."""

    def __init__(self) -> None:
        self._last_rankings: list[RotationDecision] = []

    def rank(
        self,
        candidates: list[RotationCandidate],
        mode: SystemMode,
        *,
        max_slots: int = 10,
    ) -> list[RotationDecision]:
        """
        Score and rank candidates, return top `max_slots`.

        Parameters
        ----------
        candidates : list[RotationCandidate]
            Available opportunities with their metric scores.
        mode : SystemMode
            Current operating mode.
        max_slots : int
            Maximum number of opportunities to recommend.
        """
        weights = _WEIGHT_PROFILES[mode]
        scored: list[tuple[float, RotationCandidate]] = []

        for c in candidates:
            composite = (
                weights[0] * c.adjusted_edge
                + weights[1] * c.robustness_score
                + weights[2] * c.diversification_value
                + weights[3] * c.capital_efficiency
                + weights[4] * c.regime_fit
            )
            scored.append((composite, c))

        # Sort descending by composite score
        scored.sort(key=lambda x: x[0], reverse=True)

        # Top N
        top = scored[:max_slots]

        # Allocation: proportional to score
        total_score = sum(s for s, _ in top) if top else 1.0
        if total_score <= 0:
            total_score = 1.0

        decisions: list[RotationDecision] = []
        for rank_pos, (score, cand) in enumerate(top, 1):
            alloc_pct = score / total_score
            decisions.append(
                RotationDecision(
                    setup_key=cand.setup_key,
                    composite_score=round(score, 4),
                    rank=rank_pos,
                    favored=rank_pos <= max(1, max_slots // 2),
                    reason=f"alloc={alloc_pct:.1%} w=[{weights[0]:.2f},{weights[1]:.2f},{weights[2]:.2f},{weights[3]:.2f},{weights[4]:.2f}]",
                )
            )

        self._last_rankings = decisions
        return decisions

    def get_last_rankings(self) -> list[RotationDecision]:
        return list(self._last_rankings)

    def get_top_n_keys(self, n: int = 3) -> list[str]:
        """Return the setup keys of the top N ranked opportunities."""
        return [d.setup_key for d in self._last_rankings[:n]]
