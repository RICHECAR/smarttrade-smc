import os
import sys
import json
import re
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(project_root))

from smc_futures.smc_config import SMC_CONFIG
from backend.services.stats_service import StatsService
from backend.services.live_trades_service import LiveTradesService
from scripts.reporting.config_manager import ConfigManager
from scripts.reporting.adaptive_weight_engine import (
    evaluate_adaptive_weight,
    compute_confidence,
    compute_cooldown_ok,
    load_state,
    save_state,
    record_decision,
)
from scripts.reporting.capital_scaling_engine import (
    evaluate_scaling,
    effective_max_risk,
    effective_risk_pct,
    load_scaling_state,
    save_scaling_state,
    record_scaling_decision,
)
from scripts.reporting.execution_quality_analyzer import (
    analyze_execution_quality,
    is_taker_blocking_promotion,
    is_taker_blocking_scaleup,
)
from scripts.reporting.regime_awareness import (
    evaluate_regime_awareness,
    load_regime_state,
    save_regime_state,
    record_regime_decision,
)
from scripts.reporting.governance_report import (
    generate_governance_report,
    save_governance_report,
    REPORT_WEEKLY,
    REPORT_MONTHLY,
)
from scripts.reporting.handsoff_config import (
    GOVERNANCE_SCHEDULE,
    check_alerts,
)
from smc_futures.risk.kill_switch import RiskKillSwitch

# Configure basic logging for the script
logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("DailyReporter")

# Add project root to sys.path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(project_root))

class DailyReporter:
    def __init__(self, target_date=None):
        self.target_date = target_date or date.today()
        self.date_str = self.target_date.strftime("%Y-%m-%d")
        self.log_file = project_root / "orchestrator.log"
        self.template_file = project_root / "reports" / "daily_report_template.md"
        self.output_dir = project_root / "reports" / "daily"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.stats = {
            "rejections": {"RANGE_TOO_LARGE": 0, "STALE_REJECT": 0, "NO_VALID_SETUP": 0, "RISK_BLOCKED": 0},
            "balance": {"equity_start": 0.0, "equity_end": 0.0, "change_pct": 0.0, "7d_pct": 0.0, "30d_pct": 0.0},
            "performance": {},
            "asia_performance": {},
            "ab_test": {
                "total_decisions": 0,
                "static_accepts": 0, 
                "adaptive_accepts": 0, 
                "conflicts": 0, 
                "virtual_pnl_b": 0.0,
                "mode": "off",
                "rejection_reasons_a": {},
                "rejection_reasons_b": {}
            },
            "adaptive_weight": {
                "current_weight": SMC_CONFIG.get("ADAPTIVE_LIVE_WEIGHT", 0.0),
                "decision": "HOLD",
                "confidence": "LOW",
                "reason": "",
                "new_weight": SMC_CONFIG.get("ADAPTIVE_LIVE_WEIGHT", 0.0),
                "last_change_ts": "\u2014",
                "last_decision": "HOLD",
            },
            "status": "ACTIVE",
            "reason": "All systems nominal",
            "kill_switch": {
                "daily_loss_pct": 0.0,
                "max_daily_loss_pct": SMC_CONFIG.get("MAX_DAILY_LOSS_PCT", 2.0),
                "daily_pnl": 0.0,
                "max_daily_loss_abs": SMC_CONFIG.get("MAX_DAILY_LOSS_ABS", 0.0),
                "session_loss_pct_asia": 0.0,
                "max_session_loss_pct_asia": SMC_CONFIG.get("MAX_SESSION_LOSS_PCT_ASIA", 1.0),
                "consec_losses": 0,
                "max_consec_losses": SMC_CONFIG.get("MAX_CONSECUTIVE_LOSSES_ASIA", 6),
                "block_count": 0,
                "status": "OK",
            },
            "capital_scaling": {
                "current_risk_pct": 0.5,
                "eff_max_risk": 0.8,
                "decision": "HOLD",
                "reason": "Not evaluated",
                "pf_30d": 0.0,
                "dd_30d": 0.0,
                "trades_30d": 0,
            },
            "exec_blocked": {
                "spread_tight": 0,
                "global_risk": 0,
                "insufficient_balance": 0,
                "stale_data": 0,
                "other": 0,
                "total": 0,
            },
            "exec_quality": {
                "maker": {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0, "avg_fees": 0.0, "net_pnl": 0.0, "avg_slippage": 0.0},
                "taker": {"trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0, "avg_fees": 0.0, "net_pnl": 0.0, "avg_slippage": 0.0},
                "fallback_rate": 0.0,
                "taker_degrading_edge": False,
                "taker_degrading_reason": None,
                "warnings": [],
            },
            "regime": {
                "regime": "RANGE",
                "regime_reason": "Not evaluated",
                "volatility_state": "NORMAL",
                "vol_pause_active": False,
                "recovery_active": False,
                "recovery_step": 0,
                "recovery_total_steps": 4,
                "adaptive_weight_cap": 0.5,
                "scaling_cap_factor": 1.0,
                "risk_reduction_factor": 1.0,
                "freeze_adaptive": False,
                "block_scale_up": False,
                "effective_regime": "RANGE",
                "warnings": [],
            },
        }

    def parse_logs(self):
        """Extract rejections and balance history from logs."""
        if not self.log_file.exists():
            logger.warning(f"Log file {self.log_file} not found.")
            return

        rejection_pattern = re.compile(r"\[SIGNAL_REJECTED\].*reasons=([^ |]*)")
        stale_pattern = re.compile(r"stale|STALE")
        balance_pattern = re.compile(r"BALANCE_HISTORY_AGG.*({.*})")

        with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                # Basic rejection parsing
                if "[SIGNAL_REJECTED]" in line:
                    match = rejection_pattern.search(line)
                    if match:
                        reason = match.group(1)
                        if "RANGE_TOO_LARGE" in reason: self.stats["rejections"]["RANGE_TOO_LARGE"] += 1
                        elif "STALE" in reason or "stale" in line: self.stats["rejections"]["STALE_REJECT"] += 1
                        else: self.stats["rejections"]["NO_VALID_SETUP"] += 1

                # Execution-blocked parsing (signals accepted but rejected at exec)
                if "[ORDER_REJECTED]" in line or "[AUTO_EXEC] PRE-FLIGHT REJECT" in line or "[MULTI_EXEC] PRE-FLIGHT REJECT" in line:
                    eb = self.stats["exec_blocked"]
                    eb["total"] += 1
                    if "SPREAD_TOO_TIGHT" in line or "MAKER_NOT_VIABLE" in line or "MAKER→TAKER" in line:
                        eb["spread_tight"] += 1
                    elif "GLOBAL_RISK" in line:
                        eb["global_risk"] += 1
                    elif "INSUFFICIENT_BALANCE" in line or "BALANCE" in line:
                        eb["insufficient_balance"] += 1
                    elif "STALE_DATA" in line:
                        eb["stale_data"] += 1
                    else:
                        eb["other"] += 1

                # Risk-engine specific rejections (also execution blocks)
                if "[RISK] Intent REJECTED" in line:
                    eb = self.stats["exec_blocked"]
                    eb["total"] += 1
                    if "SPREAD" in line:
                        eb["spread_tight"] += 1
                    elif "STALE" in line:
                        eb["stale_data"] += 1
                    else:
                        eb["global_risk"] += 1
                
                # Balance history parsing (JSON)
                if "BALANCE_HISTORY_AGG" in line:
                    match = balance_pattern.search(line)
                    if match:
                        try:
                            data = json.loads(match.group(1))
                            days = data.get("days")
                            if days == 7: self.stats["balance"]["7d_pct"] = data.get("change_pct", 0.0)
                            elif days == 30: self.stats["balance"]["30d_pct"] = data.get("change_pct", 0.0)
                            
                            # Use the most recent 24h/7d entry for end equity
                            self.stats["balance"]["equity_end"] = data.get("end_equity", 0.0)
                            self.stats["balance"]["equity_start"] = data.get("start_equity", 0.0)
                            self.stats["balance"]["change_pct"] = data.get("change_pct", 0.0)
                        except:
                            pass

    def fetch_performance(self):
        """Fetch trade performance from DB."""
        try:
            summary = StatsService.summary(self.target_date, self.target_date)
            self.stats["performance"] = {
                "n_trades": summary.trades,
                "pnl_abs": summary.pnl,
                "pnl_pct": 0.0, # Needs capital context
                "winrate": summary.win_rate * 100,
                "max_dd": summary.max_dd,
                "profit_factor": summary.profit_factor
            }
            
            # Session breakdown
            sessions = StatsService.session_breakdown(self.target_date, self.target_date)
            asia = sessions.get("asia", sessions.get("Asia", None)) # Handle casing
            if asia:
                self.stats["asia_performance"] = {
                    "trades": asia.trades,
                    "winrate": asia.win_rate * 100,
                    "pnl": asia.pnl
                }
        except Exception as e:
            logger.error(f"Error fetching performance: {e}")

    def evaluate_decision(self):
        """Apply the Decision Matrix logic."""
        performance = self.stats.get("performance", {})
        total_trades = performance.get("n_trades", 0)
        
        asia = self.stats.get("asia_performance", {})
        asia_trades = asia.get("trades", 0)
        asia_winrate = asia.get("winrate", 0)
        
        # 🔹 A) Explicit NO_DATA handling
        if total_trades == 0:
            self.stats["status"] = "ACTIVE"
            self.stats["reason"] = "NO_DATA - No trades executed for this period."
            return

        # ── Asia Decision Logic (from Matrix) ──
        if asia_trades >= 5: # Threshold for daily warning sensitivity
            if asia_winrate < 40:
                self.stats["status"] = "ROLLBACK_RECOMMENDED"
                self.stats["reason"] = f"Asia winrate critical: {asia_winrate:.1f}%"
            elif asia_winrate < 45:
                self.stats["status"] = "UNDER_REVIEW"
                self.stats["reason"] = f"Asia winrate underperforming: {asia_winrate:.1f}%"
        
        # ── A/B Test Analysis ──
        self.parse_ab_logs()

        # ── Adaptive Weight Decision (Phase 8 FSM) ──
        self.evaluate_adaptive_weight_step()

        # ── Kill-Switch Summary (Phase 8.2) ──
        self.evaluate_kill_switch_step()

        # ── Execution Quality: Maker vs Taker (Phase 8) ──
        self.evaluate_execution_quality_step()

        # ── Regime Awareness (Phase 9) ──
        self.evaluate_regime_step()

        # ── Capital Scaling Decision (Phase 8.3) ──
        self.evaluate_capital_scaling_step()

    def parse_ab_logs(self):
        """Parse A/B decision logs for the target date using enterprise format."""
        ab_file = project_root / "reports" / "ab_tests" / f"decisions_{self.target_date.strftime('%Y%m%d')}.jsonl"
        self.stats["ab_test"]["mode"] = SMC_CONFIG.get("ADAPTIVE_MODE", "off")
        
        if not ab_file.exists():
            return

        try:
            with open(ab_file, "r") as f:
                for line in f:
                    data = json.loads(line)
                    self.stats["ab_test"]["total_decisions"] += 1
                    
                    trace_a = data.get("A", {})
                    trace_b = data.get("B", {})
                    
                    accept_a = trace_a.get("accept", False)
                    accept_b = trace_b.get("accept", False) if trace_b else False

                    if accept_a: self.stats["ab_test"]["static_accepts"] += 1
                    else:
                        reason = trace_a.get("reason", "UNKNOWN")
                        self.stats["ab_test"]["rejection_reasons_a"][reason] = self.stats["ab_test"]["rejection_reasons_a"].get(reason, 0) + 1
                        
                    if accept_b: self.stats["ab_test"]["adaptive_accepts"] += 1
                    elif trace_b:
                        reason = trace_b.get("reason", "UNKNOWN")
                        self.stats["ab_test"]["rejection_reasons_b"][reason] = self.stats["ab_test"]["rejection_reasons_b"].get(reason, 0) + 1
                    
                    if accept_a != accept_b:
                        self.stats["ab_test"]["conflicts"] += 1
        except Exception as e:
            logger.error(f"Error parsing A/B logs: {e}")

    def evaluate_adaptive_weight_step(self):
        """Run the Adaptive Weight Engine FSM and record the decision."""
        try:
            adaptive_dir = project_root / "reports" / "adaptive"
            state = load_state(adaptive_dir)

            # Compute cooldown dynamically
            state["cooldown_ok"] = compute_cooldown_ok(state)

            current_weight = state.get("current_weight", SMC_CONFIG.get("ADAPTIVE_LIVE_WEIGHT", 0.0))

            # Build KPIs from AB decisions (placeholder: uses accept rates as proxy)
            ab = self.stats["ab_test"]
            total = ab["total_decisions"]
            perf = self.stats.get("performance", {})

            # For now, KPIs are derived from available data
            # In production, these come from StatsService filtered by engine tag
            kpis = {
                "expectancy_B": perf.get("pnl_abs", 0.0),
                "expectancy_A": perf.get("pnl_abs", 0.0),
                "profit_factor_B": perf.get("profit_factor", 0.0),
                "winrate_B": perf.get("winrate", 0.0) / 100.0 if perf.get("winrate", 0) else 0.0,
                "winrate_A": perf.get("winrate", 0.0) / 100.0 if perf.get("winrate", 0) else 0.0,
                "drawdown_B": perf.get("max_dd", 0.0),
                "drawdown_A": perf.get("max_dd", 0.0),
                "losing_streak_B": 0,
                "decisions": total,
            }

            new_weight, decision = evaluate_adaptive_weight(
                current_weight=current_weight,
                kpis_7d=kpis,
                kpis_30d=kpis,
                state=state,
                exec_quality=self.stats.get("exec_quality"),
                regime_info=self.stats.get("regime"),
            )

            confidence = compute_confidence(total, kpis)

            # Build reason text
            if decision == "PROMOTE":
                reason_text = f"All KPI gates passed. {current_weight} \u2192 {new_weight}"
            elif decision == "ROLLBACK":
                reason_text = "Safety threshold breached. Weight reset to 0.0"
            elif decision == "COOLDOWN":
                reason_text = f"Cooldown active (last: {state.get('last_decision', '?')})"
            else:
                reason_text = "KPI conditions not met for promotion. Monitoring."

            # Update stats for template rendering
            self.stats["adaptive_weight"] = {
                "current_weight": current_weight,
                "new_weight": new_weight,
                "decision": decision,
                "confidence": confidence,
                "reason": reason_text,
                "last_change_ts": state.get("last_change_ts", "\u2014"),
                "last_decision": state.get("last_decision", "HOLD"),
            }

            # Persist state if weight changed
            if new_weight != current_weight:
                state["current_weight"] = new_weight
                state["last_change_ts"] = datetime.now(timezone.utc).isoformat()
                state["last_decision"] = decision
                save_state(state, adaptive_dir)

                # Record audit trail
                record_decision(
                    decision=decision,
                    previous_weight=current_weight,
                    new_weight=new_weight,
                    kpis_7d=kpis,
                    kpis_30d=kpis,
                    confidence=confidence,
                    reason=reason_text,
                    base_dir=adaptive_dir,
                )

                logger.info(
                    f"[ADAPTIVE_WEIGHT] {decision}: {current_weight} \u2192 {new_weight} "
                    f"(confidence={confidence})"
                )
            else:
                logger.info(f"[ADAPTIVE_WEIGHT] {decision}: weight stays at {current_weight}")

        except Exception as e:
            logger.error(f"[ADAPTIVE_WEIGHT] Evaluation failed: {e}")
            import traceback
            traceback.print_exc()

    def evaluate_execution_quality_step(self):
        """Analyze Maker vs Taker execution quality."""
        try:
            # Fetch closed trades for the target date from the DB
            closed_trades = []
            try:
                trades_raw = LiveTradesService.list_closed(
                    from_date=self.target_date,
                    to_date=self.target_date,
                )
                for t in (trades_raw or []):
                    td = t if isinstance(t, dict) else (t.__dict__ if hasattr(t, '__dict__') else {})
                    closed_trades.append({
                        "pnl": float(td.get("pnl", td.get("realized_pnl", 0.0)) or 0.0),
                        "pnl_gross": float(td.get("pnl_gross", td.get("pnl", 0.0)) or 0.0),
                        "fees": float(td.get("fees", td.get("fee", 0.0)) or 0.0),
                        "r_multiple": float(td.get("r_multiple", 0.0) or 0.0),
                        "mae": float(td.get("mae", 0.0) or 0.0),
                        "slippage": float(td.get("slippage", 0.0) or 0.0),
                        "maker_fallback_used": bool(td.get("maker_fallback_used", False)),
                        "is_win": float(td.get("pnl", td.get("realized_pnl", 0.0)) or 0.0) > 0,
                    })
            except Exception as e:
                logger.warning(f"[EXEC_QUALITY] Could not fetch closed trades: {e}")

            result = analyze_execution_quality(closed_trades)
            self.stats["exec_quality"] = result

            if result.get("taker_degrading_edge"):
                logger.warning(
                    f"[EXEC_QUALITY] TAKER_DEGRADING_EDGE active: "
                    f"{result.get('taker_degrading_reason')}"
                )

        except Exception as e:
            logger.error(f"[EXEC_QUALITY] Evaluation failed: {e}")

    def evaluate_regime_step(self):
        """Evaluate market regime awareness (Phase 9)."""
        try:
            regime_dir = project_root / "reports" / "regime"
            regime_state = load_regime_state(regime_dir)

            perf = self.stats.get("performance", {})
            exec_q = self.stats.get("exec_quality", {})
            dd_pct = perf.get("max_dd", 0.0)
            fallback_rate = exec_q.get("fallback_rate", 0.0)

            # ATR ratio from strategy logs (placeholder — uses config or 1.0)
            atr_ratio = SMC_CONFIG.get("LAST_ATR_RATIO", 1.0)
            adx_value = SMC_CONFIG.get("LAST_ADX", 0.0)
            bos_total = SMC_CONFIG.get("LAST_BOS_TOTAL", 0)
            bos_confirmed = SMC_CONFIG.get("LAST_BOS_CONFIRMED", 0)

            result = evaluate_regime_awareness(
                adx=adx_value,
                atr_ratio=atr_ratio,
                bos_total=bos_total,
                bos_confirmed=bos_confirmed,
                fallback_rate=fallback_rate,
                current_vol_pause=regime_state.get("vol_pause_active", False),
                current_dd_pct=dd_pct,
                consecutive_win_days=SMC_CONFIG.get("CONSECUTIVE_WIN_DAYS", 0),
                recovery_pf=perf.get("profit_factor", 0.0),
                recovery_state=regime_state.get("recovery_state"),
            )

            self.stats["regime"] = {
                "regime": result["regime"],
                "regime_reason": result["regime_reason"],
                "volatility_state": result["volatility_state"],
                "vol_pause_active": result["vol_pause_active"],
                "recovery_active": result["recovery_active"],
                "recovery_step": result.get("recovery_step", 0),
                "recovery_total_steps": result.get("recovery_total_steps", 4),
                "adaptive_weight_cap": result["adaptive_weight_cap"],
                "scaling_cap_factor": result["scaling_cap_factor"],
                "risk_reduction_factor": result["risk_reduction_factor"],
                "freeze_adaptive": result["freeze_adaptive"],
                "block_scale_up": result["block_scale_up"],
                "effective_regime": result["effective_regime"],
                "warnings": result.get("warnings", []),
            }

            # Persist regime state
            new_regime_state = {
                "vol_pause_active": result["vol_pause_active"],
                "recovery_state": result["details"]["recovery"].get("state", {
                    "active": False, "step": 0, "days_at_step": 0, "entry_dd_pct": 0.0,
                }),
            }
            save_regime_state(new_regime_state, regime_dir)
            record_regime_decision(result, regime_dir)

            logger.info(
                f"[REGIME] {result['effective_regime']}: "
                f"cap={result['adaptive_weight_cap']}, "
                f"scale={result['scaling_cap_factor']}"
            )

        except Exception as e:
            logger.error(f"[REGIME] Evaluation failed: {e}")
            import traceback
            traceback.print_exc()

    def evaluate_kill_switch_step(self):
        """Summarise kill-switch events for the daily report."""
        try:
            ks_log_dir = project_root / "reports" / "risk"
            date_str = self.target_date.strftime("%Y%m%d")
            log_path = ks_log_dir / f"kill_switch_{date_str}.jsonl"

            block_count = 0
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if entry.get("status") == "BLOCKED":
                                block_count += 1
                        except json.JSONDecodeError:
                            pass

            perf = self.stats.get("performance", {})
            pnl = perf.get("pnl_abs", 0.0)
            equity = self.stats["balance"].get("equity_start", 0.0)
            daily_loss_pct = abs(min(pnl, 0)) / equity * 100 if equity > 0 else 0.0

            self.stats["kill_switch"]["daily_loss_pct"] = round(daily_loss_pct, 2)
            self.stats["kill_switch"]["daily_pnl"] = round(pnl, 2)
            self.stats["kill_switch"]["block_count"] = block_count
            self.stats["kill_switch"]["status"] = "BLOCKED" if block_count > 0 else "OK"

        except Exception as e:
            logger.error(f"[KILL_SWITCH] Report step failed: {e}")

    def evaluate_capital_scaling_step(self):
        """Run the capital scaling engine and update stats for the report."""
        try:
            scaling_dir = project_root / "reports" / "scaling"
            state = load_scaling_state(scaling_dir)
            current_risk = state.get("current_risk_pct", 0.5)

            perf = self.stats.get("performance", {})
            adaptive_weight = SMC_CONFIG.get("ADAPTIVE_LIVE_WEIGHT", 0.0)

            kpis_30d = {
                "profit_factor": perf.get("profit_factor", 0.0),
                "max_dd_pct": perf.get("max_dd", 0.0),
                "trades": perf.get("n_trades", 0),
                "winrate": perf.get("winrate", 0.0) / 100.0 if perf.get("winrate") else 0.0,
            }

            risk_max = SMC_CONFIG.get("RISK_PCT_MAX", 0.8)
            eff_max = effective_max_risk(risk_max, adaptive_weight)

            new_pct, decision, reason = evaluate_scaling(
                current_risk_pct=current_risk,
                kpis_30d=kpis_30d,
                adaptive_weight=adaptive_weight,
                state=state,
                exec_quality=self.stats.get("exec_quality"),
                kill_switch_active=self.stats["kill_switch"]["status"] == "BLOCKED",
                regime_info=self.stats.get("regime"),
            )

            eff_risk = effective_risk_pct(new_pct, adaptive_weight)

            self.stats["capital_scaling"] = {
                "current_risk_pct": round(new_pct, 4),
                "effective_risk_pct": round(eff_risk, 4),
                "eff_max_risk": round(eff_max, 4),
                "decision": decision,
                "reason": reason,
                "pf_30d": round(kpis_30d.get("profit_factor", 0.0), 2),
                "dd_30d": round(kpis_30d.get("max_dd_pct", 0.0), 2),
                "trades_30d": kpis_30d.get("trades", 0),
            }

            if decision != "HOLD":
                state["current_risk_pct"] = new_pct
                state["last_change_ts"] = datetime.now(timezone.utc).isoformat()
                state["last_decision"] = decision
                save_scaling_state(state, scaling_dir)

                record_scaling_decision(
                    decision=decision,
                    previous=current_risk,
                    new=new_pct,
                    reason=reason,
                    kpis_30d=kpis_30d,
                    adaptive_weight=adaptive_weight,
                    eff_max=eff_max,
                    base_dir=scaling_dir,
                )
                logger.info(f"[SCALING] {decision}: {current_risk} → {new_pct} ({reason})")
            else:
                logger.info(f"[SCALING] HOLD: risk stays at {current_risk}")

        except Exception as e:
            logger.error(f"[SCALING] Report step failed: {e}")
            import traceback
            traceback.print_exc()

    def generate_report(self):
        """Render the Markdown and JSON reports."""
        if not self.template_file.exists():
            logger.error(f"Template {self.template_file} not found.")
            return

        with open(self.template_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Simple replacement mapping
        replacements = {
            "{{DATE}}": self.date_str,
            "{{paper | live}}": SMC_CONFIG.get("TRADING_MODE", "unknown"),
            "{{start_ts}}": f"{self.date_str} 00:00:00",
            "{{end_ts}}": f"{self.date_str} 23:59:59",
            "{{n_trades}}": str(self.stats["performance"].get("n_trades", 0)),
            "{{pnl_abs}}": f"{self.stats['performance'].get('pnl_abs', 0.0):.2f}",
            "{{pnl_pct}}": f"{self.stats['performance'].get('pnl_pct', 0.0):.2f}",
            "{{winrate}}": f"{self.stats['performance'].get('winrate', 0.0):.1f}",
            "{{max_dd}}": f"{self.stats['performance'].get('max_dd', 0.0):.2f}",
            "{{ACTIVE | UNDER_REVIEW | ROLLBACK_RECOMMENDED}}": self.stats["status"],
            "{{OK | WARNING | CRITICAL}}": "OK" if self.stats["status"] == "ACTIVE" else ("CRITICAL" if "ROLLBACK" in self.stats["status"] else "WARNING"),
            "{{reason}}": self.stats["reason"],
            "{{range_count}}": str(self.stats["rejections"]["RANGE_TOO_LARGE"]),
            "{{stale_count}}": str(self.stats["rejections"]["STALE_REJECT"]),
            "{{setup_count}}": str(self.stats["rejections"]["NO_VALID_SETUP"]),
            "{{risk_count}}": str(self.stats["rejections"]["RISK_BLOCKED"]),
            "{{equity_start}}": f"{self.stats['balance']['equity_start']:.2f}",
            "{{equity_end}}": f"{self.stats['balance']['equity_end']:.2f}",
            "{{delta_abs}}": f"{self.stats['balance']['equity_end'] - self.stats['balance']['equity_start']:.2f}",
            "{{mode}}": self.stats["ab_test"]["mode"],
            "{{total_decisions}}": str(self.stats["ab_test"]["total_decisions"]),
            "{{static_accepts}}": str(self.stats["ab_test"]["static_accepts"]),
            "{{adaptive_accepts}}": str(self.stats["ab_test"]["adaptive_accepts"]),
            "{{conflicts}}": str(self.stats["ab_test"]["conflicts"]),
            "{{conflict_rate}}": f"{ (self.stats['ab_test']['conflicts'] / self.stats['ab_test']['total_decisions'] * 100) if self.stats['ab_test']['total_decisions'] > 0 else 0.0 :.1f}",
            "{{pnl_b}}": f"{self.stats['ab_test']['virtual_pnl_b']:.2f}",
            "{{commit_hash}}": "N/A",
            # \u2500\u2500 Adaptive Weight placeholders \u2500\u2500
            "{{adaptive_mode}}": self.stats["ab_test"]["mode"],
            "{{adaptive_weight}}": f"{self.stats['adaptive_weight']['current_weight'] * 100:.0f}",
            "{{last_weight_change_ts}}": self.stats["adaptive_weight"]["last_change_ts"],
            "{{last_weight_decision}}": self.stats["adaptive_weight"]["last_decision"],
            "{{A_decisions}}": str(self.stats["ab_test"]["total_decisions"]),
            "{{B_decisions}}": str(self.stats["ab_test"]["total_decisions"]),
            "{{delta_accepts}}": str(self.stats["ab_test"]["adaptive_accepts"] - self.stats["ab_test"]["static_accepts"]),
            "{{weight_decision}}": self.stats["adaptive_weight"]["decision"],
            "{{confidence_level}}": self.stats["adaptive_weight"]["confidence"],
            "{{decision_reason}}": self.stats["adaptive_weight"]["reason"],
            "{{threshold_status}}": "ROLLED_BACK" if self.stats["status"] == "ROLLBACK_RECOMMENDED" else "ACTIVE",
            "{{last_rollback_date}}": self._get_last_rollback_date(),
            "{{free_text_recommendation}}": self._build_recommendation_text(),
            # ── Kill-Switch placeholders (Phase 8.2) ──
            "{{ks_daily_loss_pct}}": f"{self.stats['kill_switch']['daily_loss_pct']:.2f}",
            "{{ks_max_daily_loss_pct}}": f"{self.stats['kill_switch']['max_daily_loss_pct']:.1f}",
            "{{ks_daily_pnl}}": f"{self.stats['kill_switch']['daily_pnl']:.2f}",
            "{{ks_max_daily_loss_abs}}": f"{self.stats['kill_switch']['max_daily_loss_abs']:.2f}",
            "{{ks_session_loss_pct_asia}}": f"{self.stats['kill_switch']['session_loss_pct_asia']:.2f}",
            "{{ks_max_session_loss_pct_asia}}": f"{self.stats['kill_switch']['max_session_loss_pct_asia']:.1f}",
            "{{ks_consec_losses}}": str(self.stats["kill_switch"]["consec_losses"]),
            "{{ks_max_consec_losses}}": str(self.stats["kill_switch"]["max_consec_losses"]),
            "{{ks_block_count}}": str(self.stats["kill_switch"]["block_count"]),
            "{{ks_status}}": self.stats["kill_switch"]["status"],
            # ── Capital Scaling placeholders (Phase 8.3) ──
            "{{sc_current_risk_pct}}": f"{self.stats['capital_scaling']['current_risk_pct']:.2f}",
            "{{sc_eff_max_risk}}": f"{self.stats['capital_scaling']['eff_max_risk']:.2f}",
            "{{sc_decision}}": self.stats["capital_scaling"]["decision"],
            "{{sc_reason}}": self.stats["capital_scaling"]["reason"],
            "{{sc_pf_30d}}": f"{self.stats['capital_scaling']['pf_30d']:.2f}",
            "{{sc_dd_30d}}": f"{self.stats['capital_scaling']['dd_30d']:.2f}",
            "{{sc_trades_30d}}": str(self.stats["capital_scaling"]["trades_30d"]),
            # ── Exec-Blocked placeholders ──
            "{{eb_spread_tight}}": str(self.stats["exec_blocked"]["spread_tight"]),
            "{{eb_global_risk}}": str(self.stats["exec_blocked"]["global_risk"]),
            "{{eb_insufficient_balance}}": str(self.stats["exec_blocked"]["insufficient_balance"]),
            "{{eb_stale_data}}": str(self.stats["exec_blocked"]["stale_data"]),
            "{{eb_other}}": str(self.stats["exec_blocked"]["other"]),
            "{{eb_total}}": str(self.stats["exec_blocked"]["total"]),
            # ── Execution Quality: Maker vs Taker placeholders ──
            "{{eq_maker_trades}}": str(self.stats["exec_quality"]["maker"]["trades"]),
            "{{eq_maker_wr}}": f"{self.stats['exec_quality']['maker']['win_rate']*100:.1f}",
            "{{eq_maker_pf}}": f"{self.stats['exec_quality']['maker']['profit_factor']:.2f}",
            "{{eq_maker_avg_r}}": f"{self.stats['exec_quality']['maker']['avg_r']:.2f}",
            "{{eq_maker_fees}}": f"{self.stats['exec_quality']['maker']['avg_fees']:.4f}",
            "{{eq_maker_pnl}}": f"{self.stats['exec_quality']['maker']['net_pnl']:.2f}",
            "{{eq_maker_slip}}": f"{self.stats['exec_quality']['maker']['avg_slippage']:.2f}",
            "{{eq_taker_trades}}": str(self.stats["exec_quality"]["taker"]["trades"]),
            "{{eq_taker_wr}}": f"{self.stats['exec_quality']['taker']['win_rate']*100:.1f}",
            "{{eq_taker_pf}}": f"{self.stats['exec_quality']['taker']['profit_factor']:.2f}",
            "{{eq_taker_avg_r}}": f"{self.stats['exec_quality']['taker']['avg_r']:.2f}",
            "{{eq_taker_fees}}": f"{self.stats['exec_quality']['taker']['avg_fees']:.4f}",
            "{{eq_taker_pnl}}": f"{self.stats['exec_quality']['taker']['net_pnl']:.2f}",
            "{{eq_taker_slip}}": f"{self.stats['exec_quality']['taker']['avg_slippage']:.2f}",
            "{{eq_fallback_rate}}": f"{self.stats['exec_quality']['fallback_rate']*100:.1f}",
            "{{eq_taker_flag}}": "🔴 ACTIVE" if self.stats["exec_quality"]["taker_degrading_edge"] else "🟢 OK",
            "{{eq_warnings}}": "; ".join(self.stats["exec_quality"]["warnings"]) or "None",
            # ── Regime Awareness placeholders (Phase 9) ──
            "{{rg_regime}}": self.stats["regime"]["regime"],
            "{{rg_reason}}": self.stats["regime"]["regime_reason"],
            "{{rg_effective}}": self.stats["regime"]["effective_regime"],
            "{{rg_vol_state}}": self.stats["regime"]["volatility_state"],
            "{{rg_vol_pause}}": "🔴 YES" if self.stats["regime"]["vol_pause_active"] else "🟢 NO",
            "{{rg_recovery}}": "🔴 YES" if self.stats["regime"]["recovery_active"] else "🟢 NO",
            "{{rg_recovery_step}}": str(self.stats["regime"]["recovery_step"]),
            "{{rg_recovery_total}}": str(self.stats["regime"]["recovery_total_steps"]),
            "{{rg_adaptive_cap}}": f"{self.stats['regime']['adaptive_weight_cap']*100:.0f}",
            "{{rg_scale_cap}}": f"{self.stats['regime']['scaling_cap_factor']:.2f}",
            "{{rg_risk_factor}}": f"{self.stats['regime']['risk_reduction_factor']:.4f}",
            "{{rg_freeze}}": "🔴 YES" if self.stats["regime"]["freeze_adaptive"] else "🟢 NO",
            "{{rg_block_scale}}": "🔴 YES" if self.stats["regime"]["block_scale_up"] else "🟢 NO",
            "{{rg_warnings}}": "; ".join(self.stats["regime"]["warnings"]) or "None",
        }

        # Update Session Table Summary (Simple fallback)
        asia = self.stats.get("asia_performance", {})
        if asia:
            asia_row = f"| Asia    | {asia.get('trades', 0)}   | {asia.get('winrate', 0.0):.1f}%    | {asia.get('pnl', 0.0):.2f} |"
            content = content.replace("| Asia    | {{}}   | {{}}    | {{}} |", asia_row)

        for key, val in replacements.items():
            content = content.replace(key, val)

        report_path = self.output_dir / f"daily_report_{self.target_date.strftime('%Y%m%d')}.md"
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        # 🔹 C) Enriched JSON metadata (report_meta)
        report_data = {
            "report_meta": {
                "date": self.date_str,
                "env": SMC_CONFIG.get("TRADING_MODE", "unknown"),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "script_version": "1.1.0"
            },
            "data": self.stats
        }

        json_path = report_path.with_suffix(".json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=4)

        logger.info(f"Report generated: {report_path}")

        logger.info(f"Report generated: {report_path}")

    def _get_last_rollback_date(self) -> str:
        """Return the date of the last rollback, or 'none'."""
        rollback_dir = project_root / "reports" / "rollbacks"
        if not rollback_dir.exists():
            return "none"
        entries = sorted(rollback_dir.glob("rollback_*.json"))
        if not entries:
            return "none"
        # Extract date from filename: rollback_YYYYMMDD_HHMMSS.json
        name = entries[-1].stem  # rollback_YYYYMMDD_HHMMSS
        try:
            date_part = name.replace("rollback_", "")[:8]
            return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
        except Exception:
            return "unknown"

    def _build_recommendation_text(self) -> str:
        """Generate human-readable recommendation based on engine decision."""
        aw = self.stats.get("adaptive_weight", {})
        decision = aw.get("decision", "HOLD")
        confidence = aw.get("confidence", "LOW")
        current = aw.get("current_weight", 0.0)
        new_w = aw.get("new_weight", 0.0)

        if decision == "PROMOTE":
            return (
                f"Adaptive engine promoted {current*100:.0f}% → {new_w*100:.0f}%. "
                f"Confidence: {confidence}. Continue monitoring KPIs over next 48h."
            )
        if decision == "ROLLBACK":
            return (
                "Safety thresholds breached — weight reset to 0% (shadow mode). "
                "Investigate root cause before re-enabling."
            )
        if decision == "COOLDOWN":
            return (
                f"Cooldown active — weight stays at {current*100:.0f}%. "
                "Wait for cooldown to elapse before next evaluation."
            )
        # HOLD
        if confidence == "LOW":
            return "Insufficient data for promotion. Continue accumulating shadow trades."
        return f"Conditions not met for promotion. Weight stays at {current*100:.0f}%. Keep monitoring."

    def run(self, auto_rollback: bool = False, dry_run: bool = False):
        logger.info(f"Running daily report for {self.date_str} (auto_rollback={auto_rollback}, dry_run={dry_run})")
        self.parse_logs()
        self.fetch_performance()
        self.evaluate_decision()
        
        # ── Task 2: Auto-rollback logic ──
        if auto_rollback and self.stats["status"] == "ROLLBACK_RECOMMENDED":
            config_mgr = ConfigManager(project_root / "smc_futures" / "smc_config.py")
            if config_mgr.rollback_asia_thresholds(reason=self.stats["reason"], dry_run=dry_run):
                action_str = "[DRY-RUN] ROLLBACK PLAN LOGGED" if dry_run else "[AUTO-ROLLBACK EXECUTED]"
                self.stats["reason"] += f" | {action_str}"
                logger.warning(f"[AUTO-ROLLBACK] {action_str}")
        
        self.generate_report()
        
        # ── Phase 10: Trigger governance reports on schedule ──
        self._maybe_generate_governance_reports()

    def _maybe_generate_governance_reports(self):
        """Generate weekly/monthly governance reports if schedule matches today."""
        try:
            reports_base = project_root / "reports"
            today = self.target_date

            # Weekly: generate on configured day (default Sunday=6)
            if GOVERNANCE_SCHEDULE.get("weekly_report") and today.weekday() == GOVERNANCE_SCHEDULE.get("weekly_day", 6):
                report = generate_governance_report(reports_base, today, REPORT_WEEKLY)
                save_governance_report(report, reports_base, REPORT_WEEKLY)
                logger.info(f"[GOVERNANCE] Weekly report generated for week ending {today}")

            # Monthly: generate on configured day (default 1st)
            if GOVERNANCE_SCHEDULE.get("monthly_report") and today.day == GOVERNANCE_SCHEDULE.get("monthly_day", 1):
                report = generate_governance_report(reports_base, today, REPORT_MONTHLY)
                save_governance_report(report, reports_base, REPORT_MONTHLY)
                logger.info(f"[GOVERNANCE] Monthly report generated for month ending {today}")

        except Exception as e:
            logger.warning(f"[GOVERNANCE] Failed to generate governance report: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Daily Trading Report")
    parser.add_argument("date", nargs="?", help="Target date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--auto-rollback", action="store_true", help="Automatically revert thresholds if KPIs fail")
    parser.add_argument("--dry-run", action="store_true", help="Log rollback decision without modifying config")
    args = parser.parse_args()
    
    target_date = None
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            logger.error("Invalid date format. Use YYYY-MM-DD")
            sys.exit(1)
    
    reporter = DailyReporter(target_date)
    reporter.run(auto_rollback=args.auto_rollback, dry_run=args.dry_run)
