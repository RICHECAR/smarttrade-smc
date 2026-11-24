"""
Doctrine Validity Engine
════════════════════════

Determines whether the current global trading doctrine is still valid
under present market conditions.

Evaluates mismatches between:
  1. Current meta-control mode ↔ actual market regime
  2. Active setups ↔ regime suitability
  3. Market microstructure ↔ setup assumptions
  4. Execution reality ↔ edge expectations
  5. Portfolio stress ↔ risk budget

Outputs a validity score (0–1) and an action recommendation:
  VALID   — doctrine confirmed, continue
  DEGRADE — partial mismatch, tighten parameters
  FREEZE  — significant mismatch, freeze new positions
  ROTATE  — doctrine invalid, rotate strategy mix

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.structural_break_models import (
    DoctrineAction,
    DoctrineMismatch,
    DoctrineVerdict,
)


class DoctrineValidityEngine:
    """Evaluate whether the current trading doctrine is still valid."""

    # Mismatch severity weights per dimension
    W_MODE_REGIME = 0.25
    W_SETUP_REGIME = 0.20
    W_MICROSTRUCTURE = 0.15
    W_EXECUTION = 0.20
    W_PORTFOLIO = 0.20

    def __init__(self) -> None:
        self._last_verdict: DoctrineVerdict | None = None

    def evaluate(
        self,
        *,
        # Mode vs regime
        current_mode: str = "selective",
        market_regime: str = "trending",
        mode_regime_alignment: float = 1.0,   # 0–1
        # Setups vs regime
        active_setup_count: int = 0,
        regime_suitable_count: int = 0,
        # Microstructure
        spread_normal: bool = True,
        volatility_normal: bool = True,
        liquidity_normal: bool = True,
        # Execution reality
        avg_fill_quality: float = 1.0,
        avg_edge_degradation: float = 0.0,      # 0–1 (0=no degradation)
        # Portfolio stress
        drawdown_pct: float = 0.0,
        portfolio_stress_score: float = 0.0,     # 0–1
        correlation_risk: float = 0.0,           # 0–1
    ) -> DoctrineVerdict:
        """
        Evaluate doctrine validity across 5 dimensions.

        Returns DoctrineVerdict with validity_score and action.
        """
        mismatches: list[DoctrineMismatch] = []

        # 1. Mode vs regime alignment
        mode_score = mode_regime_alignment
        if mode_score < 0.5:
            mismatches.append(DoctrineMismatch(
                dimension="mode_vs_regime",
                expected=f"mode={current_mode} aligned",
                actual=f"regime={market_regime} alignment={mode_score:.2f}",
                severity=1.0 - mode_score,
            ))

        # 2. Setups vs regime
        if active_setup_count > 0:
            setup_score = regime_suitable_count / active_setup_count
        else:
            setup_score = 1.0
        if setup_score < 0.5:
            mismatches.append(DoctrineMismatch(
                dimension="setups_vs_regime",
                expected=f"{active_setup_count} setups suited",
                actual=f"only {regime_suitable_count} suited ({setup_score:.0%})",
                severity=1.0 - setup_score,
            ))

        # 3. Microstructure
        micro_checks = [spread_normal, volatility_normal, liquidity_normal]
        micro_score = sum(micro_checks) / len(micro_checks)
        if micro_score < 0.67:
            broken = []
            if not spread_normal:
                broken.append("spread")
            if not volatility_normal:
                broken.append("volatility")
            if not liquidity_normal:
                broken.append("liquidity")
            mismatches.append(DoctrineMismatch(
                dimension="microstructure",
                expected="normal conditions",
                actual=f"degraded: {', '.join(broken)}",
                severity=1.0 - micro_score,
            ))

        # 4. Execution reality
        exec_score = avg_fill_quality * (1.0 - avg_edge_degradation)
        exec_score = max(0.0, min(1.0, exec_score))
        if exec_score < 0.5:
            mismatches.append(DoctrineMismatch(
                dimension="execution_reality",
                expected="fill_quality>0.7 edge_degradation<0.3",
                actual=f"fill={avg_fill_quality:.2f} degradation={avg_edge_degradation:.2f}",
                severity=1.0 - exec_score,
            ))

        # 5. Portfolio stress
        port_score = max(0.0, 1.0 - (
            0.4 * min(1.0, drawdown_pct / 0.20)
            + 0.3 * portfolio_stress_score
            + 0.3 * correlation_risk
        ))
        if port_score < 0.5:
            mismatches.append(DoctrineMismatch(
                dimension="portfolio_stress",
                expected="dd<10% stress<0.3 corr<0.3",
                actual=f"dd={drawdown_pct:.1%} stress={portfolio_stress_score:.2f} corr={correlation_risk:.2f}",
                severity=1.0 - port_score,
            ))

        # Composite validity
        composite = (
            self.W_MODE_REGIME * mode_score
            + self.W_SETUP_REGIME * setup_score
            + self.W_MICROSTRUCTURE * micro_score
            + self.W_EXECUTION * exec_score
            + self.W_PORTFOLIO * port_score
        )
        composite = max(0.0, min(1.0, composite))

        # Action determination
        if composite >= 0.75:
            action = DoctrineAction.VALID
        elif composite >= 0.50:
            action = DoctrineAction.DEGRADE
        elif composite >= 0.30:
            action = DoctrineAction.FREEZE
        else:
            action = DoctrineAction.ROTATE

        verdict = DoctrineVerdict(
            validity_score=round(composite, 4),
            action=action,
            mismatches=mismatches,
            mismatch_count=len(mismatches),
            detail=f"mode={mode_score:.2f} setup={setup_score:.2f} micro={micro_score:.2f} exec={exec_score:.2f} port={port_score:.2f}",
        )
        self._last_verdict = verdict
        return verdict

    def get_last_verdict(self) -> DoctrineVerdict | None:
        return self._last_verdict
