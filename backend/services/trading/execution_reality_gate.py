"""
Execution Reality Gate — Final gate preventing false-edge promotion
═══════════════════════════════════════════════════════════════════

Prevents promotion of setups that look good statistically but fail in
real execution. Combines signals from:
  • ExecutionQualityAnalyzer  → quality_score
  • ExecutionAdjustedEdgeEngine → edge_degradation, edge_positive
  • FillReliabilityMonitor → reliability_level
  • CapacityStressAnalyzer → capacity_ok

Decision matrix (asymmetric — fast downgrade, slow promotion):
  PASS       — all green
  WARN       — edge degraded >30% but still positive
  DOWNGRADE  — reliability degraded OR edge >50% consumed
  BLOCK      — reliability blocked OR adjusted edge negative

Safety rules:
  1. Any setup with negative adjusted edge → BLOCK (no exceptions)
  2. Reliability BLOCKED → BLOCK immediately
  3. Edge degradation > 80% → DOWNGRADE minimum
  4. Quality score < 0.3 → DOWNGRADE minimum
  5. Fewer than 10 execution records → PASS with insufficient_data flag

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.execution_models import (
    AdjustedEdgeReport,
    CapacityReport,
    ExecutionGateDecision,
    ExecutionGateVerdict,
    ExecutionQualityScore,
    ReliabilityLevel,
    ReliabilityReport,
)


class ExecutionRealityGate:
    """Final gate: block setups with false edge from execution costs."""

    # Thresholds
    MIN_RECORDS = 10
    QUALITY_DOWNGRADE = 0.30
    QUALITY_WARN = 0.55
    EDGE_DEGRADE_BLOCK = 1.0        # edge fully consumed
    EDGE_DEGRADE_DOWNGRADE = 0.50
    EDGE_DEGRADE_WARN = 0.30
    CAPACITY_MIN_SCORE = 0.40

    def __init__(self) -> None:
        pass

    # ──────────────────────────────────────────────────────
    #  PUBLIC
    # ──────────────────────────────────────────────────────

    def evaluate(
        self,
        setup_key: str,
        quality: ExecutionQualityScore,
        edge: AdjustedEdgeReport,
        reliability: ReliabilityReport,
        capacity: CapacityReport | None = None,
    ) -> ExecutionGateVerdict:
        """Evaluate setup through the execution reality gate."""

        reasons: list[str] = []
        decision = ExecutionGateDecision.PASS

        # ── Safety rule 5: insufficient data ─────────────
        if edge.trade_count < self.MIN_RECORDS:
            return ExecutionGateVerdict(
                setup_key=setup_key,
                decision=ExecutionGateDecision.PASS,
                execution_quality=quality.quality_score,
                edge_degradation=edge.edge_degradation,
                reliability_level=reliability.level.value,
                capacity_ok=True,
                reason="insufficient_data (<10 execution records)",
            )

        # ── Safety rule 1: negative adjusted edge → BLOCK ─
        if not edge.edge_positive:
            return ExecutionGateVerdict(
                setup_key=setup_key,
                decision=ExecutionGateDecision.BLOCK,
                execution_quality=quality.quality_score,
                edge_degradation=edge.edge_degradation,
                reliability_level=reliability.level.value,
                capacity_ok=self._capacity_ok(capacity),
                reason="adjusted_edge_negative",
            )

        # ── Safety rule 2: reliability BLOCKED → BLOCK ────
        if reliability.level == ReliabilityLevel.BLOCKED:
            decision = ExecutionGateDecision.BLOCK
            reasons.append("reliability_blocked")

        # ── Safety rule 3: edge degradation ───────────────
        if edge.edge_degradation >= self.EDGE_DEGRADE_DOWNGRADE:
            decision = self._worst(decision, ExecutionGateDecision.DOWNGRADE)
            reasons.append(f"edge_degraded_{edge.edge_degradation:.0%}")
        elif edge.edge_degradation >= self.EDGE_DEGRADE_WARN:
            decision = self._worst(decision, ExecutionGateDecision.WARN)
            reasons.append(f"edge_warn_{edge.edge_degradation:.0%}")

        # ── Safety rule 4: quality score ──────────────────
        if quality.quality_score < self.QUALITY_DOWNGRADE:
            decision = self._worst(decision, ExecutionGateDecision.DOWNGRADE)
            reasons.append(f"quality_low_{quality.quality_score:.2f}")
        elif quality.quality_score < self.QUALITY_WARN:
            decision = self._worst(decision, ExecutionGateDecision.WARN)
            reasons.append(f"quality_warn_{quality.quality_score:.2f}")

        # ── Reliability WARNING/DEGRADED ──────────────────
        if reliability.level == ReliabilityLevel.DEGRADED:
            decision = self._worst(decision, ExecutionGateDecision.DOWNGRADE)
            reasons.append("reliability_degraded")
        elif reliability.level == ReliabilityLevel.WARNING:
            decision = self._worst(decision, ExecutionGateDecision.WARN)
            reasons.append("reliability_warning")

        # ── Capacity check ────────────────────────────────
        cap_ok = self._capacity_ok(capacity)
        if not cap_ok:
            decision = self._worst(decision, ExecutionGateDecision.WARN)
            reasons.append("capacity_stress")

        reason = "; ".join(reasons) if reasons else "all_checks_passed"

        return ExecutionGateVerdict(
            setup_key=setup_key,
            decision=decision,
            execution_quality=quality.quality_score,
            edge_degradation=edge.edge_degradation,
            reliability_level=reliability.level.value,
            capacity_ok=cap_ok,
            reason=reason,
        )

    # ──────────────────────────────────────────────────────
    #  INTERNAL
    # ──────────────────────────────────────────────────────

    _DECISION_ORDER = {
        ExecutionGateDecision.PASS: 0,
        ExecutionGateDecision.WARN: 1,
        ExecutionGateDecision.DOWNGRADE: 2,
        ExecutionGateDecision.BLOCK: 3,
    }

    def _worst(
        self,
        a: ExecutionGateDecision,
        b: ExecutionGateDecision,
    ) -> ExecutionGateDecision:
        if self._DECISION_ORDER[b] > self._DECISION_ORDER[a]:
            return b
        return a

    def _capacity_ok(self, capacity: CapacityReport | None) -> bool:
        if capacity is None:
            return True
        return capacity.capacity_score >= self.CAPACITY_MIN_SCORE
