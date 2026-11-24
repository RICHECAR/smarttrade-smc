"""
Shock Propagation Analyzer
══════════════════════════

Detects whether local weakness in one symbol/setup is propagating into
system-wide fragility.

Propagation dimensions:
  • Symbols     — how many symbols show correlated stress
  • Families    — are multiple setup families degrading together
  • Clusters    — are correlated-asset clusters all under stress
  • Sessions    — does stress span multiple trading sessions
  • Execution   — is execution quality degrading across the board

Scope classification:
  LOCAL    — ≤ 1 dimension affected
  CLUSTER  — 2–3 dimensions affected
  SYSTEMIC — 4+ dimensions affected

Output includes propagation_score (0–1) and containment recommendation.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.structural_break_models import (
    PropagationResult,
    ShockScope,
)


class ShockPropagationAnalyzer:
    """Analyze whether local shocks are becoming systemic."""

    # Thresholds for declaring a dimension "affected"
    SYMBOL_STRESS_THRESHOLD = 0.30      # ≥30% of symbols stressed
    FAMILY_STRESS_THRESHOLD = 0.30
    CLUSTER_STRESS_THRESHOLD = 0.40
    SESSION_STRESS_THRESHOLD = 0.50
    EXECUTION_STRESS_THRESHOLD = 0.40

    def __init__(self) -> None:
        self._last_result: PropagationResult | None = None

    def analyze(
        self,
        *,
        # Symbol dimension
        total_symbols: int = 1,
        stressed_symbols: list[str] | None = None,
        # Family dimension
        total_families: int = 1,
        stressed_families: list[str] | None = None,
        # Cluster dimension
        total_clusters: int = 1,
        stressed_clusters: int = 0,
        # Session dimension
        total_sessions: int = 1,
        stressed_sessions: list[str] | None = None,
        # Execution dimension
        execution_stress_pct: float = 0.0,  # 0–1
    ) -> PropagationResult:
        """
        Analyze shock propagation across 5 dimensions.

        Returns PropagationResult with scope, score, and containment.
        """
        stressed_sym = stressed_symbols or []
        stressed_fam = stressed_families or []
        stressed_sess = stressed_sessions or []

        # Compute ratios per dimension
        sym_ratio = len(stressed_sym) / max(1, total_symbols)
        fam_ratio = len(stressed_fam) / max(1, total_families)
        cluster_ratio = stressed_clusters / max(1, total_clusters)
        sess_ratio = len(stressed_sess) / max(1, total_sessions)
        exec_ratio = execution_stress_pct

        # Which dimensions are affected
        dim_affected = 0
        if sym_ratio >= self.SYMBOL_STRESS_THRESHOLD:
            dim_affected += 1
        if fam_ratio >= self.FAMILY_STRESS_THRESHOLD:
            dim_affected += 1
        if cluster_ratio >= self.CLUSTER_STRESS_THRESHOLD:
            dim_affected += 1
        if sess_ratio >= self.SESSION_STRESS_THRESHOLD:
            dim_affected += 1
        if exec_ratio >= self.EXECUTION_STRESS_THRESHOLD:
            dim_affected += 1

        # Scope
        if dim_affected >= 4:
            scope = ShockScope.SYSTEMIC
        elif dim_affected >= 2:
            scope = ShockScope.CLUSTER
        else:
            scope = ShockScope.LOCAL

        # Propagation score: weighted average of dimension ratios
        propagation = (
            0.25 * sym_ratio
            + 0.20 * fam_ratio
            + 0.20 * cluster_ratio
            + 0.15 * sess_ratio
            + 0.20 * exec_ratio
        )
        propagation = max(0.0, min(1.0, propagation))

        # Containment
        if propagation < 0.20:
            containment = "contained"
        elif propagation < 0.50:
            containment = "partial"
        else:
            containment = "none"

        details = (
            f"dims={dim_affected}/5 sym={sym_ratio:.0%} fam={fam_ratio:.0%} "
            f"cluster={cluster_ratio:.0%} sess={sess_ratio:.0%} exec={exec_ratio:.0%}"
        )

        result = PropagationResult(
            scope=scope,
            propagation_score=round(propagation, 4),
            affected_symbols=stressed_sym,
            affected_families=stressed_fam,
            affected_sessions=stressed_sess,
            containment=containment,
            detail=details,
        )
        self._last_result = result
        return result

    def get_last_result(self) -> PropagationResult | None:
        return self._last_result
