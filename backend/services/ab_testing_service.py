import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class ABTestingService:
    """
    Tracks and compares decisions and virtual performance of 
    Static (A) vs Adaptive (B) trading engines.
    """
    
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)

    def record_decision(self, symbol: str, data: Dict[str, Any]):
        """
        Record an A/B decision for analysis using the enterprise event format.
        """
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        file_path = self.storage_path / f"decisions_{date_str}.jsonl"
        
        # Determine session - this should ideally be passed in data, 
        # but we can try to infer or use the key parts.
        key_parts = data.get("key", "").split("|")
        session = key_parts[1] if len(key_parts) > 1 else "unknown"

        entry = {
            "event": "AB_DECISION",
            "mode": data.get("mode"),
            "selected": data.get("selected"),
            "symbol": symbol,
            "session": session,
            "A": data.get("A"),
            "B": data.get("B"),
            "key": data.get("key"),
            "ts": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"[AB_TEST] Failed to record decision: {e}")

    def record_virtual_trade(self, symbol: str, engine: str, trade_data: Dict[str, Any]):
        """
        Track a virtual trade (Shadow Trading).
        Only writes if ADAPTIVE_SHADOW_WRITE_TRADES is enabled in configuration.
        """
        from smc_futures.smc_config import SMC_CONFIG
        if not SMC_CONFIG.get("ADAPTIVE_SHADOW_WRITE_TRADES", False):
            return

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        file_path = self.storage_path / f"virtual_trades_{date_str}.jsonl"
        
        entry = {
            "symbol": symbol,
            "engine": engine,
            **trade_data,
            "recorded_at": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"[AB_TEST] Failed to record virtual trade: {e}")

    # ── Report Generation ─────────────────────────────────────────────

    def load_decisions(self, date_str_yyyymmdd: str) -> List[Dict[str, Any]]:
        """Load all decisions for a given date."""
        file_path = self.storage_path / f"decisions_{date_str_yyyymmdd}.jsonl"
        decisions: List[Dict[str, Any]] = []
        if not file_path.exists():
            return decisions
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        decisions.append(json.loads(line))
        except Exception as e:
            logger.error(f"[AB_TEST] Failed to load decisions: {e}")
        return decisions

    def generate_ab_report(self, target_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Generate a comprehensive A/B testing report for the target date.

        Args:
            target_date: Date string YYYYMMDD. Defaults to today.

        Returns:
            Dict with full report data, also writes .md and .json files.
        """
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y%m%d")

        decisions = self.load_decisions(target_date)

        total = len(decisions)
        static_accepts = 0
        adaptive_accepts = 0
        conflicts = 0
        symbols_seen: Dict[str, int] = {}
        sessions_seen: Dict[str, int] = {}
        rejection_reasons_a: Dict[str, int] = {}
        rejection_reasons_b: Dict[str, int] = {}
        current_mode = "off"

        for d in decisions:
            current_mode = d.get("mode", current_mode)
            sym = d.get("symbol", "UNKNOWN")
            symbols_seen[sym] = symbols_seen.get(sym, 0) + 1

            key_parts = d.get("key", "").split("|")
            session = key_parts[1] if len(key_parts) > 1 else "unknown"
            sessions_seen[session] = sessions_seen.get(session, 0) + 1

            trace_a = d.get("A", {})
            trace_b = d.get("B")

            a_accept = trace_a.get("accept", False) if trace_a else False
            b_accept = trace_b.get("accept", False) if trace_b else False

            if a_accept:
                static_accepts += 1
            else:
                r = trace_a.get("reason", "UNKNOWN") if trace_a else "NO_TRACE"
                rejection_reasons_a[r] = rejection_reasons_a.get(r, 0) + 1

            if trace_b is not None:
                if b_accept:
                    adaptive_accepts += 1
                else:
                    r = trace_b.get("reason", "UNKNOWN")
                    rejection_reasons_b[r] = rejection_reasons_b.get(r, 0) + 1

            if a_accept != b_accept:
                conflicts += 1

        accept_rate_a = (static_accepts / total * 100) if total > 0 else 0.0
        accept_rate_b = (adaptive_accepts / total * 100) if total > 0 else 0.0
        conflict_rate = (conflicts / total * 100) if total > 0 else 0.0

        # Recommendation logic
        if total < 10:
            recommendation = "hold"
            rec_reason = f"Insufficient data ({total} decisions). Need >=10 for meaningful comparison."
        elif accept_rate_b > accept_rate_a + 5 and conflicts < total * 0.3:
            recommendation = "increase_weight"
            rec_reason = (
                f"Adaptive more permissive (+{accept_rate_b - accept_rate_a:.1f}pp) "
                f"with low conflict rate ({conflict_rate:.1f}%)."
            )
        elif accept_rate_a > accept_rate_b + 10:
            recommendation = "revert"
            rec_reason = (
                f"Adaptive too restrictive (-{accept_rate_a - accept_rate_b:.1f}pp). "
                f"Consider reverting."
            )
        else:
            recommendation = "hold"
            rec_reason = "Engines within 5pp. Continue monitoring."

        report_data = {
            "date": target_date,
            "mode": current_mode,
            "total_decisions": total,
            "static_accepts": static_accepts,
            "adaptive_accepts": adaptive_accepts,
            "accept_rate_a": round(accept_rate_a, 1),
            "accept_rate_b": round(accept_rate_b, 1),
            "conflicts": conflicts,
            "conflict_rate": round(conflict_rate, 1),
            "rejection_reasons_a": rejection_reasons_a,
            "rejection_reasons_b": rejection_reasons_b,
            "symbols": symbols_seen,
            "sessions": sessions_seen,
            "recommendation": recommendation,
            "recommendation_reason": rec_reason,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Write JSON report
        json_path = self.storage_path / f"ab_{target_date}.json"
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(report_data, f, indent=2)
        except Exception as e:
            logger.error(f"[AB_REPORT] Failed to write JSON: {e}")

        # Write Markdown report
        md_path = self.storage_path / f"ab_{target_date}.md"
        try:
            md = self._render_ab_markdown(report_data)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md)
            logger.info(f"[AB_REPORT] Generated: {md_path}")
        except Exception as e:
            logger.error(f"[AB_REPORT] Failed to write Markdown: {e}")

        return report_data

    @staticmethod
    def _render_ab_markdown(data: Dict[str, Any]) -> str:
        """Render the A/B report as Markdown."""
        lines = [
            f"# A/B Testing Report - {data['date']}",
            "",
            f"**Mode:** {data['mode']}",
            f"**Generated:** {data['generated_at']}",
            "",
            "---",
            "",
            "## Summary",
            "",
            "| Metric | Value |",
            "| ------ | ----- |",
            f"| Total Decisions | {data['total_decisions']} |",
            f"| Static (A) Accepts | {data['static_accepts']} ({data['accept_rate_a']}%) |",
            f"| Adaptive (B) Accepts | {data['adaptive_accepts']} ({data['accept_rate_b']}%) |",
            f"| Conflicts (A!=B) | {data['conflicts']} ({data['conflict_rate']}%) |",
            "",
            "## Rejection Reasons",
            "",
            "### Engine A (Static)",
            "",
            "| Reason | Count |",
            "| ------ | ----- |",
        ]

        for reason, count in sorted(
            data.get("rejection_reasons_a", {}).items(), key=lambda x: -x[1]
        ):
            lines.append(f"| {reason} | {count} |")
        if not data.get("rejection_reasons_a"):
            lines.append("| _(none)_ | 0 |")

        lines.extend([
            "",
            "### Engine B (Adaptive)",
            "",
            "| Reason | Count |",
            "| ------ | ----- |",
        ])

        for reason, count in sorted(
            data.get("rejection_reasons_b", {}).items(), key=lambda x: -x[1]
        ):
            lines.append(f"| {reason} | {count} |")
        if not data.get("rejection_reasons_b"):
            lines.append("| _(none)_ | 0 |")

        lines.extend([
            "",
            "## Symbols Breakdown",
            "",
            "| Symbol | Decisions |",
            "| ------ | --------- |",
        ])
        for sym, cnt in sorted(data.get("symbols", {}).items()):
            lines.append(f"| {sym} | {cnt} |")

        lines.extend([
            "",
            "## Recommendation",
            "",
            f"**Action:** `{data['recommendation']}`",
            f"**Reason:** {data['recommendation_reason']}",
            "",
            "---",
            "",
            "*Auto-generated by ABTestingService*",
        ])

        return "\n".join(lines) + "\n"
