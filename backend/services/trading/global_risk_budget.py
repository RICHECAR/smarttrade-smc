"""
Global Risk Budget Controller
══════════════════════════════

Translates the current SystemMode into hard capital-risk limits:

  • max_total_exposure   — % of equity deployed (all positions summed)
  • max_concurrent       — number of simultaneous positions
  • max_per_symbol       — % cap per symbol cluster (BTC, ETH …)
  • max_per_family       — % cap per setup family (breakout, reversal …)
  • max_new_per_cycle    — max new positions per evaluation cycle
  • max_correlated       — max co-moving positions

Budget tightens automatically in DEFENSIVE / SURVIVAL and loosens in
EXPANSION / OFFENSIVE — controlled by the MetaControlEngine's mode.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

from backend.services.trading.meta_control_models import RiskBudget, SystemMode


# ── Preset budgets per mode ─────────────────────────────────────────
_BUDGETS: dict[SystemMode, dict] = {
    SystemMode.EXPANSION: {
        "max_total_exposure": 0.95,
        "max_concurrent_positions": 12,
        "max_per_symbol_cluster": 0.40,
        "max_per_setup_family": 0.30,
        "min_cash_reserve": 0.05,
        "max_single_position": 0.25,
    },
    SystemMode.OFFENSIVE: {
        "max_total_exposure": 0.80,
        "max_concurrent_positions": 10,
        "max_per_symbol_cluster": 0.35,
        "max_per_setup_family": 0.25,
        "min_cash_reserve": 0.10,
        "max_single_position": 0.22,
    },
    SystemMode.SELECTIVE: {
        "max_total_exposure": 0.60,
        "max_concurrent_positions": 7,
        "max_per_symbol_cluster": 0.30,
        "max_per_setup_family": 0.20,
        "min_cash_reserve": 0.15,
        "max_single_position": 0.20,
    },
    SystemMode.DEFENSIVE: {
        "max_total_exposure": 0.35,
        "max_concurrent_positions": 4,
        "max_per_symbol_cluster": 0.20,
        "max_per_setup_family": 0.15,
        "min_cash_reserve": 0.25,
        "max_single_position": 0.15,
    },
    SystemMode.SURVIVAL: {
        "max_total_exposure": 0.20,
        "max_concurrent_positions": 2,
        "max_per_symbol_cluster": 0.10,
        "max_per_setup_family": 0.10,
        "min_cash_reserve": 0.40,
        "max_single_position": 0.10,
    },
}


class GlobalRiskBudgetController:
    """Produce hard risk-budget caps based on the current system mode."""

    def __init__(self) -> None:
        self._last_budget: RiskBudget | None = None

    def compute(
        self,
        mode: SystemMode,
        *,
        override_exposure: float | None = None,
        override_concurrent: int | None = None,
        degradation_mult: float = 1.0,
    ) -> RiskBudget:
        """
        Return a RiskBudget for the given mode.

        Parameters
        ----------
        mode : SystemMode
            Current operating mode from MetaControlEngine.
        override_exposure : float | None
            Hard override for max_total_exposure (safety).
        override_concurrent : int | None
            Hard override for max_concurrent.
        degradation_mult : float
            0–1 multiplier from MetaHealthMonitor to further tighten
            budgets when stack health is degraded.
        """
        base = _BUDGETS[mode]
        dm = max(0.0, min(1.0, degradation_mult))

        budget = RiskBudget(
            mode=mode.value,
            max_total_exposure=min(
                base["max_total_exposure"] * dm,
                override_exposure if override_exposure is not None else 1.0,
            ),
            max_concurrent_positions=min(
                max(1, int(base["max_concurrent_positions"] * dm)),
                override_concurrent if override_concurrent is not None else 999,
            ),
            max_per_symbol_cluster=base["max_per_symbol_cluster"] * dm,
            max_per_setup_family=base["max_per_setup_family"] * dm,
            min_cash_reserve=base["min_cash_reserve"],
            max_single_position=base["max_single_position"] * dm,
        )
        self._last_budget = budget
        return budget

    def get_last_budget(self) -> RiskBudget | None:
        return self._last_budget
