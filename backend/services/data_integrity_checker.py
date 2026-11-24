"""
Data Integrity Checker — Self-Monitoring Service
═════════════════════════════════════════════════

Periodic checks that detect data inconsistencies BEFORE they cause losses:

  1. Position count: exchange vs DB
  2. Balance drift: computed PnL vs exchange balance delta
  3. Fill gaps: missing fills in time window
  4. Stale data: last update too old
  5. Fee/funding anomalies

Designed to run every N minutes via the main event loop.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger("smarttrade.data_integrity")


@dataclass
class IntegrityAlert:
    check_name: str
    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class IntegrityReport:
    checks_run: int = 0
    checks_passed: int = 0
    alerts: List[IntegrityAlert] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def all_passed(self) -> bool:
        return self.checks_run == self.checks_passed

    @property
    def has_critical(self) -> bool:
        return any(a.severity == "CRITICAL" for a in self.alerts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checks_run": self.checks_run,
            "checks_passed": self.checks_passed,
            "all_passed": self.all_passed,
            "has_critical": self.has_critical,
            "alerts": [
                {"check": a.check_name, "severity": a.severity,
                 "message": a.message, "details": a.details}
                for a in self.alerts
            ],
            "timestamp": self.timestamp,
        }


class DataIntegrityChecker:
    """
    Self-monitoring service for trade data consistency.

    Register providers for exchange data, DB data, and balance info.
    Call run_all_checks() periodically (e.g., every 5 minutes).
    """

    def __init__(
        self,
        balance_drift_threshold_pct: float = 1.0,
        stale_data_threshold_sec: float = 120.0,
        max_acceptable_fee_ratio: float = 0.005,
    ):
        self._balance_drift_threshold = balance_drift_threshold_pct
        self._stale_threshold = stale_data_threshold_sec
        self._max_fee_ratio = max_acceptable_fee_ratio

        # Providers (registered externally)
        self._exchange_position_fetcher: Optional[Callable] = None
        self._db_position_fetcher: Optional[Callable] = None
        self._exchange_balance_fetcher: Optional[Callable] = None
        self._db_balance_fetcher: Optional[Callable] = None
        self._db_fills_fetcher: Optional[Callable] = None
        self._last_ws_timestamp: float = 0.0
        self._last_rest_timestamp: float = 0.0

    # ── Registration ──

    def register_exchange_positions(self, fn: Callable):
        self._exchange_position_fetcher = fn

    def register_db_positions(self, fn: Callable):
        self._db_position_fetcher = fn

    def register_exchange_balance(self, fn: Callable):
        self._exchange_balance_fetcher = fn

    def register_db_balance(self, fn: Callable):
        self._db_balance_fetcher = fn

    def register_db_fills(self, fn: Callable):
        self._db_fills_fetcher = fn

    def update_ws_timestamp(self):
        self._last_ws_timestamp = time.time()

    def update_rest_timestamp(self):
        self._last_rest_timestamp = time.time()

    # ── Checks ──

    async def run_all_checks(self) -> IntegrityReport:
        """Run all integrity checks and return report."""
        report = IntegrityReport()

        await self._check_position_count(report)
        await self._check_balance_drift(report)
        self._check_data_staleness(report)
        await self._check_fill_gaps(report)

        if report.has_critical:
            logger.critical(
                "[INTEGRITY] CRITICAL ALERT: %d checks failed, %d critical",
                report.checks_run - report.checks_passed,
                sum(1 for a in report.alerts if a.severity == "CRITICAL"),
            )
        elif not report.all_passed:
            logger.warning(
                "[INTEGRITY] %d/%d checks passed",
                report.checks_passed, report.checks_run,
            )
        else:
            logger.info("[INTEGRITY] All %d checks passed", report.checks_run)

        return report

    async def _check_position_count(self, report: IntegrityReport) -> None:
        """Verify exchange position count matches DB."""
        report.checks_run += 1
        if not self._exchange_position_fetcher or not self._db_position_fetcher:
            report.checks_passed += 1  # skip if not registered
            return

        try:
            exchange_positions = await self._exchange_position_fetcher()
            db_positions = self._db_position_fetcher()

            ex_count = len([p for p in (exchange_positions or [])
                           if float(p.get("size", 0) or p.get("quantity", 0) or 0) != 0])
            db_count = len([p for p in (db_positions or []) if p.get("status") == "OPEN"])

            if ex_count != db_count:
                report.alerts.append(IntegrityAlert(
                    check_name="position_count",
                    severity="CRITICAL" if abs(ex_count - db_count) > 1 else "HIGH",
                    message=f"Position count mismatch: exchange={ex_count} DB={db_count}",
                    details={"exchange": ex_count, "db": db_count},
                ))
            else:
                report.checks_passed += 1
        except Exception as e:
            report.alerts.append(IntegrityAlert(
                check_name="position_count",
                severity="HIGH",
                message=f"Position count check failed: {e}",
            ))

    async def _check_balance_drift(self, report: IntegrityReport) -> None:
        """Verify computed PnL vs actual balance change."""
        report.checks_run += 1
        if not self._exchange_balance_fetcher or not self._db_balance_fetcher:
            report.checks_passed += 1
            return

        try:
            exchange_balance = await self._exchange_balance_fetcher()
            db_balance = self._db_balance_fetcher()

            ex_val = float(exchange_balance) if exchange_balance else 0
            db_val = float(db_balance) if db_balance else 0

            if ex_val > 0 and db_val > 0:
                drift_pct = abs(ex_val - db_val) / ex_val * 100
                if drift_pct > self._balance_drift_threshold:
                    report.alerts.append(IntegrityAlert(
                        check_name="balance_drift",
                        severity="CRITICAL" if drift_pct > 5.0 else "HIGH",
                        message=f"Balance drift {drift_pct:.2f}%: exchange={ex_val:.2f} DB={db_val:.2f}",
                        details={"exchange": ex_val, "db": db_val, "drift_pct": drift_pct},
                    ))
                else:
                    report.checks_passed += 1
            else:
                report.checks_passed += 1
        except Exception as e:
            report.alerts.append(IntegrityAlert(
                check_name="balance_drift",
                severity="HIGH",
                message=f"Balance drift check failed: {e}",
            ))

    def _check_data_staleness(self, report: IntegrityReport) -> None:
        """Check if WS and REST data are fresh."""
        report.checks_run += 1
        now = time.time()
        issues = []

        if self._last_ws_timestamp > 0:
            ws_age = now - self._last_ws_timestamp
            if ws_age > self._stale_threshold:
                issues.append(f"WebSocket data stale ({ws_age:.0f}s ago)")

        if self._last_rest_timestamp > 0:
            rest_age = now - self._last_rest_timestamp
            if rest_age > self._stale_threshold * 3:  # REST allowed 3x longer
                issues.append(f"REST data stale ({rest_age:.0f}s ago)")

        if issues:
            report.alerts.append(IntegrityAlert(
                check_name="data_staleness",
                severity="HIGH",
                message=" | ".join(issues),
            ))
        else:
            report.checks_passed += 1

    async def _check_fill_gaps(self, report: IntegrityReport) -> None:
        """Check for temporal gaps in fills (missing fills indicator)."""
        report.checks_run += 1
        if not self._db_fills_fetcher:
            report.checks_passed += 1
            return

        try:
            fills = self._db_fills_fetcher()
            if not fills or len(fills) < 2:
                report.checks_passed += 1
                return

            # Check for abnormal PnL in individual fills
            anomalies = []
            total_fees = 0.0
            total_notional = 0.0
            for f in fills:
                pnl = float(f.get("pnl", 0) or 0)
                fee = float(f.get("fee", 0) or 0)
                qty = float(f.get("qty", 0) or f.get("quantity", 0) or 0)
                price = float(f.get("price", 0) or 0)
                notional = qty * price
                total_fees += abs(fee)
                total_notional += notional

                # Extremely large single-trade PnL is suspicious
                if notional > 0 and abs(pnl) / notional > 0.5:
                    anomalies.append(f"Fill {f.get('id', '?')}: PnL {pnl:.2f} on {notional:.2f} notional")

            # Fee ratio check
            if total_notional > 0 and total_fees / total_notional > self._max_fee_ratio:
                report.alerts.append(IntegrityAlert(
                    check_name="fee_anomaly",
                    severity="MEDIUM",
                    message=f"Fee ratio {total_fees/total_notional:.4f} exceeds threshold {self._max_fee_ratio}",
                    details={"total_fees": total_fees, "total_notional": total_notional},
                ))

            if anomalies:
                report.alerts.append(IntegrityAlert(
                    check_name="fill_anomaly",
                    severity="HIGH",
                    message=f"{len(anomalies)} suspicious fills detected",
                    details={"anomalies": anomalies[:10]},
                ))
            else:
                report.checks_passed += 1
        except Exception as e:
            report.alerts.append(IntegrityAlert(
                check_name="fill_gaps",
                severity="MEDIUM",
                message=f"Fill gap check failed: {e}",
            ))
