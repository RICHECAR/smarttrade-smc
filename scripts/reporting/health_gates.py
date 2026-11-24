"""
Health Gates — Phase 11C

Pre-cycle health checks that must ALL pass before the trading
engine is allowed to execute a cycle.

Gates:
  1. api_connectivity    – Exchange API reachable (ping/time endpoint)
  2. data_freshness      – Last market data < max_age_seconds
  3. state_integrity     – State files readable and valid JSON
  4. clock_sync          – System clock within tolerance of exchange
  5. risk_budget         – Remaining daily risk budget > 0
  6. kill_switch_clear   – Kill-switch not active
  7. disk_space          – Sufficient disk for logs/reports
  8. memory_usage        – Process memory < threshold

Each gate returns:
  - gate_id, passed, detail, severity

Design:
  - Pure functions with injectable dependencies (no real I/O in tests)
  - Configurable thresholds via GATE_THRESHOLDS
  - Gate results feed into governance reporting
  - Fail-closed: if any critical gate fails → skip cycle

Author: SMC Trading Bot Framework — Phase 11C
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 1. CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

GATE_THRESHOLDS: Dict[str, Any] = {
    "api_timeout_ms": 3000,          # max API response time
    "data_max_age_seconds": 60,      # max market data age
    "clock_max_skew_seconds": 10,    # max clock desync
    "min_disk_mb": 100,              # min free disk space (MB)
    "max_memory_mb": 1024,           # max process memory (MB)
    "min_risk_budget_pct": 0.01,     # min remaining risk budget
}


# ══════════════════════════════════════════════════════════════════════
# 2. DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════

@dataclass
class GateResult:
    """Result of a single health gate check."""
    gate_id: str
    passed: bool
    detail: str
    severity: str     # "info", "warning", "critical"
    value: Any = None


@dataclass
class HealthCheckResult:
    """Aggregate result of all health gates."""
    all_passed: bool
    critical_passed: bool
    gates: List[Dict[str, Any]]
    summary: Dict[str, Any]


# ══════════════════════════════════════════════════════════════════════
# 3. INDIVIDUAL GATE CHECKS
# ══════════════════════════════════════════════════════════════════════

def check_api_connectivity(
    api_latency_ms: Optional[float] = None,
    api_reachable: bool = True,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check if exchange API is reachable and responsive."""
    th = thresholds or GATE_THRESHOLDS
    timeout = th.get("api_timeout_ms", 3000)

    if not api_reachable:
        return GateResult(
            gate_id="api_connectivity",
            passed=False,
            detail="Exchange API unreachable",
            severity="critical",
            value=None,
        )

    if api_latency_ms is not None and api_latency_ms > timeout:
        return GateResult(
            gate_id="api_connectivity",
            passed=False,
            detail=f"API latency {api_latency_ms:.0f}ms > {timeout}ms threshold",
            severity="critical",
            value=api_latency_ms,
        )

    return GateResult(
        gate_id="api_connectivity",
        passed=True,
        detail=f"API ok (latency: {api_latency_ms:.0f}ms)" if api_latency_ms else "API reachable",
        severity="info",
        value=api_latency_ms,
    )


def check_data_freshness(
    data_age_seconds: float,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check if market data is fresh enough."""
    th = thresholds or GATE_THRESHOLDS
    max_age = th.get("data_max_age_seconds", 60)

    if data_age_seconds > max_age:
        return GateResult(
            gate_id="data_freshness",
            passed=False,
            detail=f"Data age {data_age_seconds:.0f}s > {max_age}s max",
            severity="critical",
            value=data_age_seconds,
        )

    return GateResult(
        gate_id="data_freshness",
        passed=True,
        detail=f"Data age {data_age_seconds:.0f}s (max {max_age}s)",
        severity="info",
        value=data_age_seconds,
    )


def check_state_integrity(
    state_readable: bool = True,
    state_valid: bool = True,
) -> GateResult:
    """Check if state files are readable and valid."""
    if not state_readable:
        return GateResult(
            gate_id="state_integrity",
            passed=False,
            detail="State files unreadable (IO error or locked)",
            severity="critical",
        )
    if not state_valid:
        return GateResult(
            gate_id="state_integrity",
            passed=False,
            detail="State files corrupted (invalid JSON)",
            severity="critical",
        )
    return GateResult(
        gate_id="state_integrity",
        passed=True,
        detail="State files OK",
        severity="info",
    )


def check_clock_sync(
    skew_seconds: float,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check system clock synchronization."""
    th = thresholds or GATE_THRESHOLDS
    max_skew = th.get("clock_max_skew_seconds", 10)

    if abs(skew_seconds) > max_skew:
        return GateResult(
            gate_id="clock_sync",
            passed=False,
            detail=f"Clock skew {skew_seconds:.1f}s > ±{max_skew}s",
            severity="critical",
            value=skew_seconds,
        )

    return GateResult(
        gate_id="clock_sync",
        passed=True,
        detail=f"Clock skew {skew_seconds:.1f}s within ±{max_skew}s",
        severity="info",
        value=skew_seconds,
    )


def check_risk_budget(
    remaining_risk_pct: float,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check remaining daily risk budget."""
    th = thresholds or GATE_THRESHOLDS
    min_budget = th.get("min_risk_budget_pct", 0.01)

    if remaining_risk_pct < min_budget:
        return GateResult(
            gate_id="risk_budget",
            passed=False,
            detail=f"Risk budget {remaining_risk_pct:.4f}% < {min_budget}% minimum",
            severity="critical",
            value=remaining_risk_pct,
        )

    return GateResult(
        gate_id="risk_budget",
        passed=True,
        detail=f"Risk budget {remaining_risk_pct:.4f}% remaining",
        severity="info",
        value=remaining_risk_pct,
    )


def check_kill_switch(
    kill_switch_active: bool = False,
) -> GateResult:
    """Check if kill-switch is deactivated."""
    if kill_switch_active:
        return GateResult(
            gate_id="kill_switch_clear",
            passed=False,
            detail="Kill-switch is ACTIVE — trading blocked",
            severity="critical",
        )
    return GateResult(
        gate_id="kill_switch_clear",
        passed=True,
        detail="Kill-switch clear",
        severity="info",
    )


def check_disk_space(
    free_disk_mb: float,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check free disk space for logs/reports."""
    th = thresholds or GATE_THRESHOLDS
    min_disk = th.get("min_disk_mb", 100)

    if free_disk_mb < min_disk:
        return GateResult(
            gate_id="disk_space",
            passed=False,
            detail=f"Free disk {free_disk_mb:.0f}MB < {min_disk}MB minimum",
            severity="warning",
            value=free_disk_mb,
        )

    return GateResult(
        gate_id="disk_space",
        passed=True,
        detail=f"Free disk {free_disk_mb:.0f}MB",
        severity="info",
        value=free_disk_mb,
    )


def check_memory_usage(
    process_memory_mb: float,
    thresholds: Optional[Dict] = None,
) -> GateResult:
    """Check process memory consumption."""
    th = thresholds or GATE_THRESHOLDS
    max_mem = th.get("max_memory_mb", 1024)

    if process_memory_mb > max_mem:
        return GateResult(
            gate_id="memory_usage",
            passed=False,
            detail=f"Memory {process_memory_mb:.0f}MB > {max_mem}MB max",
            severity="warning",
            value=process_memory_mb,
        )

    return GateResult(
        gate_id="memory_usage",
        passed=True,
        detail=f"Memory {process_memory_mb:.0f}MB (max {max_mem}MB)",
        severity="info",
        value=process_memory_mb,
    )


# ══════════════════════════════════════════════════════════════════════
# 4. AGGREGATE HEALTH CHECK
# ══════════════════════════════════════════════════════════════════════

def run_health_check(
    api_latency_ms: Optional[float] = None,
    api_reachable: bool = True,
    data_age_seconds: float = 0,
    state_readable: bool = True,
    state_valid: bool = True,
    skew_seconds: float = 0,
    remaining_risk_pct: float = 1.0,
    kill_switch_active: bool = False,
    free_disk_mb: float = 1000,
    process_memory_mb: float = 256,
    thresholds: Optional[Dict] = None,
) -> HealthCheckResult:
    """
    Run all health gates and aggregate results.

    Returns:
        HealthCheckResult with per-gate details and summary
    """
    th = thresholds or GATE_THRESHOLDS

    gates = [
        check_api_connectivity(api_latency_ms, api_reachable, th),
        check_data_freshness(data_age_seconds, th),
        check_state_integrity(state_readable, state_valid),
        check_clock_sync(skew_seconds, th),
        check_risk_budget(remaining_risk_pct, th),
        check_kill_switch(kill_switch_active),
        check_disk_space(free_disk_mb, th),
        check_memory_usage(process_memory_mb, th),
    ]

    gate_dicts = [asdict(g) for g in gates]

    all_passed = all(g.passed for g in gates)
    critical_passed = all(
        g.passed for g in gates if g.severity == "critical"
    )

    passed_count = sum(1 for g in gates if g.passed)
    failed_count = len(gates) - passed_count
    critical_failures = [
        g.gate_id for g in gates
        if not g.passed and g.severity == "critical"
    ]
    warning_failures = [
        g.gate_id for g in gates
        if not g.passed and g.severity == "warning"
    ]

    summary = {
        "total_gates": len(gates),
        "passed": passed_count,
        "failed": failed_count,
        "critical_failures": critical_failures,
        "warning_failures": warning_failures,
        "trade_allowed": critical_passed,
    }

    return HealthCheckResult(
        all_passed=all_passed,
        critical_passed=critical_passed,
        gates=gate_dicts,
        summary=summary,
    )


# ══════════════════════════════════════════════════════════════════════
# 5. SLO / SLI DEFINITIONS
# ══════════════════════════════════════════════════════════════════════

SLO_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "availability": {
        "description": "Trading system uptime (cycles executed / cycles scheduled)",
        "target": 99.5,   # %
        "unit": "%",
        "measurement": "cycles_executed / cycles_scheduled * 100",
    },
    "latency_p99": {
        "description": "99th percentile cycle execution time",
        "target": 5000,   # ms
        "unit": "ms",
        "measurement": "p99(cycle_duration_ms)",
    },
    "data_freshness": {
        "description": "Market data staleness at cycle start",
        "target": 60,     # seconds
        "unit": "seconds",
        "measurement": "max(data_age_seconds_at_cycle_start)",
    },
    "error_rate": {
        "description": "Percentage of cycles with unhandled errors",
        "target": 0.5,    # %
        "unit": "%",
        "measurement": "error_cycles / total_cycles * 100",
    },
    "risk_compliance": {
        "description": "Percentage of trades within risk limits",
        "target": 100.0,  # %
        "unit": "%",
        "measurement": "compliant_trades / total_trades * 100",
    },
}


def evaluate_slo(
    slo_id: str,
    actual_value: float,
) -> Dict[str, Any]:
    """
    Evaluate a single SLO against its target.

    Returns:
        {slo_id, target, actual, met, margin}
    """
    slo = SLO_DEFINITIONS.get(slo_id)
    if not slo:
        return {
            "slo_id": slo_id, "target": None, "actual": actual_value,
            "met": False, "margin": 0,
        }

    target = slo["target"]

    # For latency / freshness / error_rate: lower is better
    if slo_id in ("latency_p99", "data_freshness", "error_rate"):
        met = actual_value <= target
        margin = round(target - actual_value, 2)
    else:
        # For availability / risk_compliance: higher is better
        met = actual_value >= target
        margin = round(actual_value - target, 2)

    return {
        "slo_id": slo_id,
        "description": slo["description"],
        "target": target,
        "actual": round(actual_value, 2),
        "unit": slo["unit"],
        "met": met,
        "margin": margin,
    }


def evaluate_all_slos(
    actuals: Dict[str, float],
) -> Dict[str, Any]:
    """
    Evaluate all SLOs against actual values.

    Args:
        actuals: Dict of slo_id → actual measured value

    Returns:
        {results: [...], summary: {total, met, missed, compliance_pct}}
    """
    results = []
    for slo_id in SLO_DEFINITIONS:
        actual = actuals.get(slo_id)
        if actual is not None:
            results.append(evaluate_slo(slo_id, actual))

    met = sum(1 for r in results if r["met"])
    total = len(results)
    missed = total - met

    return {
        "results": results,
        "summary": {
            "total": total,
            "met": met,
            "missed": missed,
            "compliance_pct": round(met / total * 100, 1) if total > 0 else 0,
        },
    }


# ══════════════════════════════════════════════════════════════════════
# 6. RENDER — markdown for governance
# ══════════════════════════════════════════════════════════════════════

def render_health_section(health: HealthCheckResult) -> str:
    """Render health check results as Markdown."""
    s = health.summary
    status = "✅ ALL CLEAR" if health.all_passed else (
        "⚠️ WARNINGS" if health.critical_passed else "❌ BLOCKED"
    )

    lines = [
        "## Health Gates",
        "",
        f"**Status: {status}** — "
        f"{s['passed']}/{s['total_gates']} gates passed",
        "",
        "| Gate | Status | Severity | Detail |",
        "|------|--------|----------|--------|",
    ]

    for g in health.gates:
        icon = "✅" if g["passed"] else "❌"
        lines.append(
            f"| {g['gate_id']} | {icon} | {g['severity']} | {g['detail']} |"
        )

    if s["critical_failures"]:
        lines.append("")
        lines.append(f"**Critical failures:** {', '.join(s['critical_failures'])}")

    lines.append("")
    return "\n".join(lines)


def render_slo_section(slo_report: Dict[str, Any]) -> str:
    """Render SLO compliance as Markdown."""
    s = slo_report.get("summary", {})
    results = slo_report.get("results", [])

    lines = [
        "## SLO Compliance",
        "",
        f"**{s.get('met', 0)}/{s.get('total', 0)} SLOs met** "
        f"({s.get('compliance_pct', 0):.1f}% compliance)",
        "",
        "| SLO | Target | Actual | Met | Margin |",
        "|-----|--------|--------|-----|--------|",
    ]

    for r in results:
        icon = "✅" if r["met"] else "❌"
        lines.append(
            f"| {r['slo_id']} | {r['target']}{r.get('unit', '')} | "
            f"{r['actual']}{r.get('unit', '')} | {icon} | {r['margin']:+.2f} |"
        )

    lines.append("")
    return "\n".join(lines)
