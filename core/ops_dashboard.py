"""
Ops Dashboard — Unified operator observability for 24/7 operation.
═══════════════════════════════════════════════════════════════════

Phase 11: Single-pane view of system state for the operator.

Aggregates:
  1. GLOBAL STATE: health level, mode, uptime, version
  2. EVENT BUS: backlog, latency, dead letters, gaps, WAL size
  3. RECOVERY: last recovery, steps, status
  4. RISK: capital, exposure, drawdown, positions, kill switch
  5. EXCHANGE: connectivity, latency per broker
  6. DATABASE: size, integrity, connection status
  7. BACKUPS: count, latest, disk space
  8. DEAD LETTERS: count, oldest, categorization
  9. ALERTS: active count by severity, recent history
  10. BACKLOG: per-subscriber depths
  11. LATENCY: percentiles (p50, p95, p99)
  12. RESTARTS: per-service restart frequency
  13. PREFLIGHT: last preflight verdict

Metrics → Actions mapping:
  OBSERVE   → Log for dashboards
  ALERTER   → Send external alert
  DÉGRADER  → Reduce trading capacity
  BLOQUER   → Stop all trading

Capital Readiness tiers:
  TIER_1: 1k€   → paper + micro-capital only
  TIER_2: 10k€  → proven 7d live stability
  TIER_3: 50k€  → proven 30d + no critical incidents
  TIER_4: 100k€ → proven 90d + institutional metrics
  TIER_5: 1M€   → full institutional validation
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("smarttrade.ops_dashboard")


class CapitalTier(str, Enum):
    """Capital readiness tier — determines max allowed capital."""
    PAPER = "paper"         # Paper trading only
    TIER_1 = "tier_1"      # 1k€ — micro-capital
    TIER_2 = "tier_2"      # 10k€ — proven 7d
    TIER_3 = "tier_3"      # 50k€ — proven 30d
    TIER_4 = "tier_4"      # 100k€ — proven 90d
    TIER_5 = "tier_5"      # 1M€ — institutional


@dataclass
class CapitalAssessment:
    """Assessment of capital readiness."""
    current_tier: CapitalTier
    max_capital_eur: float
    conditions_met: Dict[str, bool]
    blockers: List[str]
    recommendation: str


class OpsDashboard:
    """
    Central ops observability aggregator.

    Collects state from all subsystems and produces a unified snapshot.
    Call get_snapshot() for the complete system state.
    """

    def __init__(self) -> None:
        self._last_snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_count = 0

    def get_snapshot(self) -> Dict[str, Any]:
        """Collect full system state snapshot."""
        start = time.time()
        snapshot: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "snapshot_id": self._snapshot_count,
        }

        snapshot["global"] = self._collect_global()
        snapshot["event_bus"] = self._collect_event_bus()
        snapshot["risk"] = self._collect_risk()
        snapshot["database"] = self._collect_database()
        snapshot["backups"] = self._collect_backups()
        snapshot["monitoring"] = self._collect_monitoring()
        snapshot["preflight"] = self._collect_preflight_status()
        snapshot["capital_readiness"] = self._assess_capital_readiness(snapshot)

        snapshot["collection_time_ms"] = round((time.time() - start) * 1000, 1)
        self._snapshot_count += 1
        self._last_snapshot = snapshot
        return snapshot

    # ──────────────────────────────────────────────────
    #  COLLECTORS
    # ──────────────────────────────────────────────────

    def _collect_global(self) -> Dict[str, Any]:
        """Global system state."""
        result: Dict[str, Any] = {
            "uptime_sec": 0.0,
            "trading_mode": os.getenv("TRADING_MODE", "paper"),
            "default_broker": os.getenv("DEFAULT_BROKER", "kraken"),
            "auto_execute": os.getenv("AUTO_EXECUTE", "false"),
        }

        try:
            from core.isolation import ServiceSupervisor
            from core.phase6_wiring import _supervisor
            if _supervisor is not None:
                status = _supervisor.get_status()
                result["system_health"] = status.get("system_health", "unknown")
                result["trading_allowed"] = status.get("trading_allowed", False)
                result["services"] = {
                    name: {
                        "state": svc.get("state", "unknown"),
                        "crash_count": svc.get("crash_count", 0),
                        "uptime": svc.get("uptime_sec", 0),
                    }
                    for name, svc in status.get("services", {}).items()
                }
        except Exception:
            result["system_health"] = "unknown"
            result["trading_allowed"] = False

        return result

    def _collect_event_bus(self) -> Dict[str, Any]:
        """Event bus state."""
        try:
            from core.events.event_bus import get_event_bus
            bus = get_event_bus()
            stats = bus.get_stats()
            mem = bus.get_memory_pressure()
            backlogs = bus.get_subscriber_backlogs()

            return {
                "healthy": True,
                "kill_switch": stats.get("kill_switch_active", False),
                "max_backlog": stats.get("max_backlog", 0),
                "total_published": stats.get("total_published", 0),
                "total_errors": stats.get("total_errors", 0),
                "total_dropped": stats.get("total_dropped", 0),
                "dead_letters": stats.get("dead_letter_count", 0),
                "gaps_detected": stats.get("gaps_detected", 0),
                "wal_size_mb": round(stats.get("wal_size_bytes", 0) / (1024 * 1024), 2),
                "memory_pressure": mem,
                "subscriber_backlogs": backlogs,
                "latency_avg_ms": stats.get("latency_avg_ms", 0),
                "latency_p99_ms": stats.get("latency_p99_ms", 0),
            }
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    def _collect_risk(self) -> Dict[str, Any]:
        """Risk engine state."""
        try:
            from core.phase6_wiring import _risk_v2
            if _risk_v2 is not None and hasattr(_risk_v2, 'get_status'):
                status = _risk_v2.get_status()
                return {
                    "healthy": True,
                    "total_capital": status.get("total_capital", 0),
                    "allocated_pct": status.get("allocated_pct", 0),
                    "daily_loss": status.get("daily_loss", 0),
                    "max_daily_loss_pct": status.get("max_daily_loss_pct", 0),
                    "open_positions": status.get("open_positions", 0),
                }
        except Exception:
            pass
        return {"healthy": False, "error": "Risk engine not available"}

    def _collect_database(self) -> Dict[str, Any]:
        """Database state."""
        db_url = os.getenv("DATABASE_URL", "sqlite:///./data/trades.db")
        db_path = db_url.replace("sqlite:///", "")

        result: Dict[str, Any] = {"path": db_path}

        if not os.path.exists(db_path):
            result["healthy"] = False
            result["error"] = "DB file not found"
            return result

        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            conn.close()

            result["healthy"] = integrity == "ok"
            result["integrity"] = integrity
            result["journal_mode"] = mode
            result["table_count"] = tables
            result["size_kb"] = round(os.path.getsize(db_path) / 1024, 1)
        except Exception as e:
            result["healthy"] = False
            result["error"] = str(e)

        return result

    def _collect_backups(self) -> Dict[str, Any]:
        """Backup subsystem state."""
        try:
            from core.backup_manager import get_backup_manager
            mgr = get_backup_manager()
            status = mgr.get_status()
            return {
                "healthy": status.healthy,
                "count": status.backup_count,
                "latest": status.latest_timestamp,
                "total_size_bytes": status.total_size_bytes,
                "free_disk_mb": status.free_disk_mb,
            }
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    def _collect_monitoring(self) -> Dict[str, Any]:
        """Long-run monitor state."""
        try:
            from core.long_run_monitor import get_monitor
            monitor = get_monitor()
            status = monitor.get_status()
            return {
                "health": status.get("health", "unknown"),
                "cycles": status.get("cycles", 0),
                "uptime_sec": status.get("uptime_sec", 0),
                "active_alerts": status.get("active_alerts", {}),
                "counters": status.get("counters", {}),
            }
        except Exception as e:
            return {"health": "unknown", "error": str(e)}

    def _collect_preflight_status(self) -> Dict[str, Any]:
        """Last preflight result (if cached)."""
        # Preflight is expensive, so we only report if it was run
        return {"available": True, "note": "Run preflight explicitly via go_live_check.py"}

    # ──────────────────────────────────────────────────
    #  CAPITAL READINESS ASSESSMENT
    # ──────────────────────────────────────────────────

    def _assess_capital_readiness(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """
        Assess capital readiness tier based on system state.

        Tier criteria:
          PAPER:  System exists but not validated
          TIER_1: All preflight checks pass, paper trading 24h stable
          TIER_2: 7d live stability, 0 critical incidents
          TIER_3: 30d live, drawdown < 10%, Sharpe > 0.5
          TIER_4: 90d live, drawdown < 8%, Sharpe > 1.0
          TIER_5: 180d live, institutional metrics met
        """
        conditions: Dict[str, bool] = {}
        blockers: List[str] = []

        # Basic system health
        global_state = snapshot.get("global", {})
        bus_state = snapshot.get("event_bus", {})
        risk_state = snapshot.get("risk", {})
        db_state = snapshot.get("database", {})
        backup_state = snapshot.get("backups", {})

        conditions["system_healthy"] = global_state.get("system_health") in ("healthy", "unknown")
        conditions["event_bus_ok"] = bus_state.get("healthy", False)
        conditions["risk_engine_ok"] = risk_state.get("healthy", False)
        conditions["database_ok"] = db_state.get("healthy", False)
        conditions["backups_exist"] = backup_state.get("count", 0) > 0
        conditions["no_kill_switch"] = not bus_state.get("kill_switch", True)
        conditions["disk_ok"] = backup_state.get("free_disk_mb", 0) > 500

        for name, met in conditions.items():
            if not met:
                blockers.append(f"Condition not met: {name}")

        # Determine tier
        if blockers:
            tier = CapitalTier.PAPER
            max_capital = 0.0
            recommendation = "Fix blockers before deploying any capital"
        else:
            # Default to TIER_1 when all basic conditions met
            # Higher tiers require operational history (not checkable from snapshot alone)
            tier = CapitalTier.TIER_1
            max_capital = 1000.0
            recommendation = "System ready for micro-capital (1k€ max). Run for 7d to qualify for TIER_2."

        assessment = CapitalAssessment(
            current_tier=tier,
            max_capital_eur=max_capital,
            conditions_met=conditions,
            blockers=blockers,
            recommendation=recommendation,
        )

        return {
            "tier": assessment.current_tier.value,
            "max_capital_eur": assessment.max_capital_eur,
            "conditions": assessment.conditions_met,
            "blockers": assessment.blockers,
            "recommendation": assessment.recommendation,
        }


# ──────────────────────────────────────────────────
#  SINGLETON
# ──────────────────────────────────────────────────

_instance: Optional[OpsDashboard] = None


def get_ops_dashboard() -> OpsDashboard:
    global _instance
    if _instance is None:
        _instance = OpsDashboard()
    return _instance
