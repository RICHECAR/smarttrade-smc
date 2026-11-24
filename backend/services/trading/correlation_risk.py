"""
Correlation Risk Engine — Detect and mitigate correlated exposure
══════════════════════════════════════════════════════════════════

Detects:
  • Pairwise correlation between symbols using return series
  • Concentration clusters (groups of highly correlated positions)
  • Hidden concentration via same-direction setups on correlated assets

Outputs haircut multipliers to reduce allocation when multiple signals
express the same underlying risk.

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from backend.services.trading.portfolio_models import (
    CorrelationLevel,
    CorrelationPair,
    CorrelationRiskReport,
)


class CorrelationRiskEngine:
    """Detect and penalize correlated exposure in the portfolio."""

    # Thresholds
    HIGH_CORR = 0.60
    EXTREME_CORR = 0.85
    MAX_CLUSTER_EXPOSURE = 0.40         # max 40% in one correlation cluster
    HAIRCUT_HIGH = 0.75                 # reduce to 75% for high correlation
    HAIRCUT_EXTREME = 0.50              # reduce to 50% for extreme

    def __init__(
        self,
        max_cluster_exposure: float = 0.40,
    ) -> None:
        self.MAX_CLUSTER_EXPOSURE = max_cluster_exposure
        self._return_series: Dict[str, List[float]] = {}

    # ────────────────────────────────────────────────────
    #  PUBLIC
    # ────────────────────────────────────────────────────

    def update_returns(self, symbol: str, returns: List[float]) -> None:
        """Feed return series for a symbol (most recent last)."""
        self._return_series[symbol] = returns

    def analyze(
        self,
        positions: Dict[str, float],  # setup_key → alloc_pct
        symbol_map: Dict[str, str],   # setup_key → symbol
    ) -> CorrelationRiskReport:
        """
        Analyze correlation risk across current positions.

        Returns haircut_map: setup_key → multiplier (0–1).
        """
        # 1. Compute pairwise correlations
        symbols = list(set(symbol_map.values()))
        pairs = self._compute_pairwise(symbols)

        # 2. Build clusters via Union-Find on HIGH+ correlated pairs
        clusters = self._build_clusters(symbols, pairs)

        # 3. Compute cluster exposures
        cluster_exposures: List[Tuple[List[str], float]] = []
        for cluster_symbols in clusters:
            total_exp = 0.0
            for sk, alloc in positions.items():
                if symbol_map.get(sk, "") in cluster_symbols:
                    total_exp += alloc
            cluster_exposures.append((cluster_symbols, total_exp))

        max_cluster = max((e for _, e in cluster_exposures), default=0.0)

        # 4. Compute haircuts per setup
        haircut_map: Dict[str, float] = {}
        for sk in positions:
            sym = symbol_map.get(sk, "")
            worst_corr = self._worst_correlation(sym, pairs)
            if worst_corr >= self.EXTREME_CORR:
                haircut_map[sk] = self.HAIRCUT_EXTREME
            elif worst_corr >= self.HIGH_CORR:
                haircut_map[sk] = self.HAIRCUT_HIGH
            else:
                haircut_map[sk] = 1.0

        # 5. Apply cluster cap: if a cluster exceeds budget, cascade reduce
        for cluster_symbols, exposure in cluster_exposures:
            if exposure > self.MAX_CLUSTER_EXPOSURE and exposure > 0:
                reduction = self.MAX_CLUSTER_EXPOSURE / exposure
                for sk in positions:
                    if symbol_map.get(sk, "") in cluster_symbols:
                        haircut_map[sk] = min(
                            haircut_map.get(sk, 1.0),
                            reduction,
                        )

        # Portfolio diversification score
        n = len(symbols)
        if n <= 1:
            div_score = 0.0
        else:
            avg_corr = (
                sum(abs(p.correlation) for p in pairs) / len(pairs)
                if pairs else 0.0
            )
            div_score = max(0.0, min(1.0, 1.0 - avg_corr))

        return CorrelationRiskReport(
            pairs=pairs,
            concentration_clusters=[cs for cs, _ in cluster_exposures if len(cs) > 1],
            max_cluster_exposure=max_cluster,
            haircut_map=haircut_map,
            portfolio_diversification_score=div_score,
        )

    # ────────────────────────────────────────────────────
    #  INTERNAL — correlation computation
    # ────────────────────────────────────────────────────

    def _compute_pairwise(self, symbols: List[str]) -> List[CorrelationPair]:
        pairs: List[CorrelationPair] = []
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                corr = self._pearson(symbols[i], symbols[j])
                level = self._classify(corr)
                pairs.append(CorrelationPair(
                    key_a=symbols[i],
                    key_b=symbols[j],
                    correlation=corr,
                    level=level,
                    shared_risk_factor=self._infer_factor(symbols[i], symbols[j], corr),
                ))
        return pairs

    def _pearson(self, sym_a: str, sym_b: str) -> float:
        """Compute Pearson correlation. Returns 0 if insufficient data."""
        ra = self._return_series.get(sym_a, [])
        rb = self._return_series.get(sym_b, [])
        n = min(len(ra), len(rb))
        if n < 5:
            return 0.0
        ra, rb = ra[-n:], rb[-n:]
        mean_a = sum(ra) / n
        mean_b = sum(rb) / n
        cov = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n))
        var_a = sum((x - mean_a) ** 2 for x in ra)
        var_b = sum((x - mean_b) ** 2 for x in rb)
        denom = math.sqrt(var_a * var_b)
        if denom < 1e-12:
            return 0.0
        return cov / denom

    @staticmethod
    def _classify(corr: float) -> CorrelationLevel:
        ac = abs(corr)
        if ac >= 0.85:
            return CorrelationLevel.EXTREME
        if ac >= 0.60:
            return CorrelationLevel.HIGH
        if ac >= 0.30:
            return CorrelationLevel.MODERATE
        return CorrelationLevel.LOW

    @staticmethod
    def _infer_factor(sym_a: str, sym_b: str, corr: float) -> str:
        if abs(corr) < 0.30:
            return "independent"
        # Simple heuristic: crypto pairs share "crypto_beta"
        crypto = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"}
        a_base = sym_a.split("/")[0].upper() if "/" in sym_a else sym_a.upper()
        b_base = sym_b.split("/")[0].upper() if "/" in sym_b else sym_b.upper()
        if a_base in crypto and b_base in crypto:
            return "crypto_beta"
        return "unknown_common_factor"

    def _build_clusters(
        self,
        symbols: List[str],
        pairs: List[CorrelationPair],
    ) -> List[List[str]]:
        """Union-Find clustering on high-correlated pairs."""
        parent: Dict[str, str] = {s: s for s in symbols}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for p in pairs:
            if abs(p.correlation) >= self.HIGH_CORR:
                union(p.key_a, p.key_b)

        clusters_map: Dict[str, List[str]] = defaultdict(list)
        for s in symbols:
            clusters_map[find(s)].append(s)
        return list(clusters_map.values())

    @staticmethod
    def _worst_correlation(
        symbol: str,
        pairs: List[CorrelationPair],
    ) -> float:
        worst = 0.0
        for p in pairs:
            if p.key_a == symbol or p.key_b == symbol:
                worst = max(worst, abs(p.correlation))
        return worst
