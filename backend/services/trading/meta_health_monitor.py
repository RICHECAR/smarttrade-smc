"""
Meta Health Monitor
═══════════════════

Aggregates health across 5 domains of the trading stack:

  1. EXECUTION     — fill quality, slippage, latency
  2. SIGNAL        — edge quality, win-rate trend, regime detection
  3. PORTFOLIO     — drawdown, correlation, concentration risk
  4. POLICY        — setup decay, allocation stability, confidence
  5. DATA          — data freshness, API errors, feed gaps

For each domain, produces a HealthStatus (HEALTHY / DEGRADED /
CRITICAL / OFFLINE).

The aggregate stack_health_score (0–1) is fed into the
MetaControlEngine as part of ModeInputs.

Also produces a degradation_multiplier (0–1) that tightens the
risk budget via GlobalRiskBudgetController when health weakens.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.meta_control_models import (
    DomainHealth,
    HealthDomain,
    HealthStatus,
    StackHealthReport,
)


_STATUS_SCORES: dict[HealthStatus, float] = {
    HealthStatus.HEALTHY: 1.0,
    HealthStatus.WARNING: 0.7,
    HealthStatus.DEGRADED: 0.4,
    HealthStatus.CRITICAL: 0.1,
}


class MetaHealthMonitor:
    """Aggregate health of the full trading stack."""

    def __init__(self) -> None:
        self._domain_health: dict[HealthDomain, DomainHealth] = {
            d: DomainHealth(
                domain=d,
                score=1.0,
                status=HealthStatus.HEALTHY,
                details="init",
            )
            for d in HealthDomain
        }

    # ────────────────────────────────────────────────────
    #  Domain-specific updates
    # ────────────────────────────────────────────────────

    def update_execution(
        self,
        avg_fill_quality: float,
        avg_latency_ms: float,
        recent_failures: int,
    ) -> DomainHealth:
        """Update execution domain health."""
        score = avg_fill_quality * 0.5
        # Latency penalty: >2000ms = critical
        latency_score = max(0.0, 1.0 - avg_latency_ms / 2000)
        score += latency_score * 0.3
        # Failure penalty
        failure_score = max(0.0, 1.0 - recent_failures / 5)
        score += failure_score * 0.2
        score = max(0.0, min(1.0, score))
        return self._set_domain(HealthDomain.EXECUTION, score,
                                f"fill={avg_fill_quality:.2f} lat={avg_latency_ms:.0f}ms fail={recent_failures}")

    def update_signal(
        self,
        avg_edge: float,
        win_rate_trend: float,
        regime_confidence: float,
    ) -> DomainHealth:
        """Update signal quality domain."""
        score = (
            0.40 * avg_edge
            + 0.30 * max(0.0, min(1.0, win_rate_trend))
            + 0.30 * regime_confidence
        )
        score = max(0.0, min(1.0, score))
        return self._set_domain(HealthDomain.SIGNAL_QUALITY, score,
                                f"edge={avg_edge:.2f} wr_trend={win_rate_trend:.2f} regime={regime_confidence:.2f}")

    def update_portfolio(
        self,
        drawdown_pct: float,
        max_correlation: float,
        concentration_pct: float,
    ) -> DomainHealth:
        """Update portfolio health domain."""
        dd_score = max(0.0, 1.0 - drawdown_pct / 0.25)
        corr_score = max(0.0, 1.0 - max_correlation)
        conc_score = max(0.0, 1.0 - concentration_pct / 0.50)
        score = 0.50 * dd_score + 0.25 * corr_score + 0.25 * conc_score
        score = max(0.0, min(1.0, score))
        return self._set_domain(HealthDomain.PORTFOLIO_STRESS, score,
                                f"dd={drawdown_pct:.1%} corr={max_correlation:.2f} conc={concentration_pct:.1%}")

    def update_policy(
        self,
        avg_decay_severity: float,
        allocation_stability: float,
        avg_confidence: float,
    ) -> DomainHealth:
        """Update policy degradation domain."""
        decay_score = max(0.0, 1.0 - avg_decay_severity / 3.0)
        score = (
            0.35 * decay_score
            + 0.30 * allocation_stability
            + 0.35 * avg_confidence
        )
        score = max(0.0, min(1.0, score))
        return self._set_domain(HealthDomain.POLICY_DEGRADATION, score,
                                f"decay={avg_decay_severity:.1f} stab={allocation_stability:.2f} conf={avg_confidence:.2f}")

    def update_data(
        self,
        data_age_seconds: float,
        api_error_rate: float,
        feed_gaps: int,
    ) -> DomainHealth:
        """Update data freshness domain."""
        age_score = max(0.0, 1.0 - data_age_seconds / 300)  # 5 min = 0
        err_score = max(0.0, 1.0 - api_error_rate)
        gap_score = max(0.0, 1.0 - feed_gaps / 10)
        score = 0.40 * age_score + 0.35 * err_score + 0.25 * gap_score
        score = max(0.0, min(1.0, score))
        return self._set_domain(HealthDomain.DATA_FRESHNESS, score,
                                f"age={data_age_seconds:.0f}s err={api_error_rate:.1%} gaps={feed_gaps}")

    # ────────────────────────────────────────────────────
    #  Aggregate report
    # ────────────────────────────────────────────────────

    def get_report(self) -> StackHealthReport:
        """Aggregate all domain health into a single report."""
        domains = list(self._domain_health.values())
        # Weighted: execution and portfolio are critical
        weights = {
            HealthDomain.EXECUTION: 0.25,
            HealthDomain.SIGNAL_QUALITY: 0.20,
            HealthDomain.PORTFOLIO_STRESS: 0.25,
            HealthDomain.POLICY_DEGRADATION: 0.15,
            HealthDomain.DATA_FRESHNESS: 0.15,
        }
        aggregate = sum(
            weights[d.domain] * d.score for d in domains
        )
        aggregate = max(0.0, min(1.0, aggregate))

        # Overall status
        min_score = min(d.score for d in domains)
        if min_score < 0.15 or aggregate < 0.2:
            overall = HealthStatus.CRITICAL
        elif min_score < 0.4 or aggregate < 0.4:
            overall = HealthStatus.DEGRADED
        elif min_score < 0.6 or aggregate < 0.6:
            overall = HealthStatus.WARNING
        else:
            overall = HealthStatus.HEALTHY

        # Degradation multiplier: 1.0 at full health, 0.3 at critical
        degradation_mult = 0.3 + 0.7 * aggregate
        degradation_active = degradation_mult < 0.95

        return StackHealthReport(
            domains=domains,
            overall_score=round(aggregate, 4),
            overall_status=overall,
            degradation_active=degradation_active,
            degradation_mult=round(degradation_mult, 4),
        )

    def get_stack_health_score(self) -> float:
        """Quick accessor for the aggregate score (0–1)."""
        return self.get_report().overall_score

    def get_degradation_multiplier(self) -> float:
        """Quick accessor for the degradation multiplier."""
        return self.get_report().degradation_mult

    # ────────────────────────────────────────────────────
    #  Internal
    # ────────────────────────────────────────────────────

    def _set_domain(
        self, domain: HealthDomain, score: float, details: str
    ) -> DomainHealth:
        status = self._score_to_status(score)
        dh = DomainHealth(
            domain=domain,
            score=round(score, 4),
            status=status,
            details=details,
        )
        self._domain_health[domain] = dh
        return dh

    @staticmethod
    def _score_to_status(score: float) -> HealthStatus:
        if score < 0.15:
            return HealthStatus.CRITICAL
        if score < 0.4:
            return HealthStatus.DEGRADED
        if score < 0.6:
            return HealthStatus.WARNING
        return HealthStatus.HEALTHY
