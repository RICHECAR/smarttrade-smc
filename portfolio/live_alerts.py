"""
Live Alerts — Threshold-based alerting for live trading.
════════════════════════════════════════════════════════
Alerts are structured, logged, and collected for reporting.
No external notification (email/SMS) — just structured output
that can be piped to any notification channel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List

from portfolio.live_config import LiveSafeConfig

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class LiveAlert:
    """Single alert event."""
    timestamp: str
    severity: AlertSeverity
    code: str
    message: str
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.code}: {self.message}"


class AlertCollector:
    """Collects alerts during a rebalance cycle. Resets each cycle."""

    def __init__(self):
        self._alerts: List[LiveAlert] = []

    def emit(self, severity: AlertSeverity, code: str,
             message: str, detail: str = "") -> None:
        alert = LiveAlert(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            severity=severity,
            code=code,
            message=message,
            detail=detail,
        )
        self._alerts.append(alert)
        if severity == AlertSeverity.CRITICAL:
            logger.critical("[LIVE_ALERT] %s", alert)
        elif severity == AlertSeverity.WARNING:
            logger.warning("[LIVE_ALERT] %s", alert)
        else:
            logger.info("[LIVE_ALERT] %s", alert)

    @property
    def alerts(self) -> List[LiveAlert]:
        return list(self._alerts)

    @property
    def has_critical(self) -> bool:
        return any(a.severity == AlertSeverity.CRITICAL for a in self._alerts)

    @property
    def has_warning(self) -> bool:
        return any(a.severity == AlertSeverity.WARNING for a in self._alerts)

    def reset(self) -> None:
        self._alerts.clear()

    def summary(self) -> List[str]:
        return [str(a) for a in self._alerts]


# ── Alert check functions ─────────────────────────────────────────

def alert_drawdown(
    current_value: float,
    peak_value: float,
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Alert on drawdown levels."""
    if peak_value <= 0:
        return
    dd = (peak_value - current_value) / peak_value
    if dd > cfg.max_dd_pct:
        collector.emit(
            AlertSeverity.CRITICAL, "DD_HALT",
            f"Drawdown {dd:.1%} exceeds max {cfg.max_dd_pct:.0%}",
            f"current=${current_value:,.0f} peak=${peak_value:,.0f}",
        )
    elif dd > cfg.max_dd_pct * 0.7:
        collector.emit(
            AlertSeverity.WARNING, "DD_HIGH",
            f"Drawdown {dd:.1%} approaching limit {cfg.max_dd_pct:.0%}",
        )


def alert_slippage(
    ticker: str,
    slippage_bps: float,
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Alert on abnormal slippage per order."""
    if abs(slippage_bps) > cfg.max_slippage_bps:
        collector.emit(
            AlertSeverity.WARNING, "SLIPPAGE_ABNORMAL",
            f"{ticker}: slippage {slippage_bps:.1f}bps > max {cfg.max_slippage_bps:.0f}bps",
        )


def alert_tracking_error(
    paper_return: float,
    live_return: float,
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Alert if paper/live return divergence is too high."""
    diff = abs(paper_return - live_return)
    if diff > cfg.max_tracking_error_pct:
        collector.emit(
            AlertSeverity.CRITICAL, "TRACKING_ERROR_HIGH",
            f"Paper/live return gap {diff:.1%} > max {cfg.max_tracking_error_pct:.0%}",
            f"paper={paper_return:+.2%} live={live_return:+.2%}",
        )
    elif diff > cfg.max_tracking_error_pct * 0.5:
        collector.emit(
            AlertSeverity.WARNING, "TRACKING_ERROR_RISING",
            f"Paper/live return gap {diff:.1%} approaching limit",
        )


def alert_weight_divergence(
    paper_weights: Dict[str, float],
    live_weights: Dict[str, float],
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Alert on any ticker weight diverging too much."""
    all_t = set(paper_weights) | set(live_weights)
    for t in all_t:
        pw = paper_weights.get(t, 0.0)
        lw = live_weights.get(t, 0.0)
        diff = abs(pw - lw)
        if diff > cfg.max_weight_divergence_pct:
            collector.emit(
                AlertSeverity.WARNING, "WEIGHT_DIVERGENCE",
                f"{t}: paper={pw:.1%} live={lw:.1%} diff={diff:.1%}",
            )


def alert_single_position_loss(
    ticker: str,
    unrealized_pnl_pct: float,
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Alert if a single position has excessive loss."""
    if unrealized_pnl_pct < -cfg.max_single_loss_pct:
        collector.emit(
            AlertSeverity.WARNING, "SINGLE_POSITION_LOSS",
            f"{ticker}: unrealized {unrealized_pnl_pct:+.1%} < -{cfg.max_single_loss_pct:.0%}",
        )


def run_all_alerts(
    *,
    current_value: float,
    peak_value: float,
    paper_return: float,
    live_return: float,
    paper_weights: Dict[str, float],
    live_weights: Dict[str, float],
    positions_pnl: Dict[str, float],
    cfg: LiveSafeConfig,
    collector: AlertCollector,
) -> None:
    """Run all alert checks. Results accumulated in collector."""
    alert_drawdown(current_value, peak_value, cfg, collector)
    alert_tracking_error(paper_return, live_return, cfg, collector)
    alert_weight_divergence(paper_weights, live_weights, cfg, collector)
    for ticker, pnl_pct in positions_pnl.items():
        alert_single_position_loss(ticker, pnl_pct, cfg, collector)
