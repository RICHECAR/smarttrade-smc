"""
Opportunity Ranking Engine
══════════════════════════

Ranks all detected opportunities globally by combining 5 dimensions:

  1. Opportunity score       (w=0.30)  — raw detection quality
  2. Execution-adjusted edge (w=0.20)  — edge after fill/slippage costs
  3. Robustness              (w=0.15)  — statistical robustness of the setup
  4. Regime alignment        (w=0.20)  — how well opportunity fits current regime
  5. Diversification benefit (w=0.15)  — portfolio diversification value

Outputs ranked list, top candidates (above threshold), and suppressed
opportunities (regime mismatch, low robustness, etc.).

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.opportunity_dominance_models import (
    OpportunitySignal,
    OpportunityType,
    RankedOpportunity,
    RankingReport,
)


class OpportunityRankingEngine:
    """Rank opportunities across multiple quality dimensions."""

    # Dimension weights
    W_OPPORTUNITY = 0.30
    W_EXECUTION = 0.20
    W_ROBUSTNESS = 0.15
    W_REGIME = 0.20
    W_DIVERSIFICATION = 0.15

    # Thresholds
    TOP_CANDIDATE_THRESHOLD = 0.60  # composite ≥ 0.60 → top candidate
    SUPPRESSION_THRESHOLD = 0.30    # composite < 0.30 → suppressed
    REGIME_SUPPRESS_BELOW = 0.25    # regime alignment < 0.25 → suppress
    ROBUSTNESS_SUPPRESS_BELOW = 0.20  # robustness < 0.20 → suppress

    def __init__(self) -> None:
        self._last_report: RankingReport | None = None

    def rank(
        self,
        opportunities: list[OpportunitySignal],
        *,
        # Per-symbol context (symbol → value)
        execution_edges: dict[str, float] | None = None,
        robustness_scores: dict[str, float] | None = None,
        regime_alignments: dict[str, float] | None = None,
        diversification_benefits: dict[str, float] | None = None,
    ) -> RankingReport:
        """
        Rank a set of detected opportunities.

        Parameters:
            opportunities: OpportunitySignal list from DetectionEngine
            execution_edges: symbol → execution-adjusted edge (0–1)
            robustness_scores: symbol → statistical robustness (0–1)
            regime_alignments: symbol → regime fit (0–1)
            diversification_benefits: symbol → diversification value (0–1)
        """
        exec_map = execution_edges or {}
        rob_map = robustness_scores or {}
        regime_map = regime_alignments or {}
        div_map = diversification_benefits or {}

        ranked_list: list[RankedOpportunity] = []

        for opp in opportunities:
            sym = opp.symbol
            exec_edge = exec_map.get(sym, 0.5)
            robustness = rob_map.get(sym, 0.5)
            regime_align = regime_map.get(sym, 0.5)
            div_benefit = div_map.get(sym, 0.5)

            # Composite score
            composite = (
                self.W_OPPORTUNITY * opp.opportunity_score
                + self.W_EXECUTION * exec_edge
                + self.W_ROBUSTNESS * robustness
                + self.W_REGIME * regime_align
                + self.W_DIVERSIFICATION * div_benefit
            )
            composite = max(0.0, min(1.0, composite))

            # Suppression checks
            suppressed = False
            suppression_reason = ""
            if regime_align < self.REGIME_SUPPRESS_BELOW:
                suppressed = True
                suppression_reason = f"regime_alignment={regime_align:.2f}<{self.REGIME_SUPPRESS_BELOW}"
            elif robustness < self.ROBUSTNESS_SUPPRESS_BELOW:
                suppressed = True
                suppression_reason = f"robustness={robustness:.2f}<{self.ROBUSTNESS_SUPPRESS_BELOW}"
            elif composite < self.SUPPRESSION_THRESHOLD:
                suppressed = True
                suppression_reason = f"composite={composite:.2f}<{self.SUPPRESSION_THRESHOLD}"

            ranked_list.append(RankedOpportunity(
                symbol=sym,
                opportunity_type=opp.opportunity_type,
                composite_rank_score=round(composite, 4),
                opportunity_score=opp.opportunity_score,
                execution_edge=round(exec_edge, 4),
                robustness=round(robustness, 4),
                regime_alignment=round(regime_align, 4),
                diversification_benefit=round(div_benefit, 4),
                suppressed=suppressed,
                suppression_reason=suppression_reason,
            ))

        # Sort by composite descending
        ranked_list.sort(key=lambda r: r.composite_rank_score, reverse=True)

        # Assign ranks
        for i, r in enumerate(ranked_list):
            r.rank = i + 1

        # Split into categories
        top = [r for r in ranked_list if not r.suppressed and r.composite_rank_score >= self.TOP_CANDIDATE_THRESHOLD]
        suppressed = [r for r in ranked_list if r.suppressed]

        report = RankingReport(
            ranked=ranked_list,
            top_candidates=top,
            suppressed=suppressed,
            total_ranked=len(ranked_list),
        )
        self._last_report = report
        return report

    def get_last_report(self) -> RankingReport | None:
        return self._last_report
