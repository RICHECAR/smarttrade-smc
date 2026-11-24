"""
Discovery Engine
════════════════

Identifies new potential edges that the system was not explicitly
designed to exploit:

  • Emerging patterns in market microstructure
  • New inter-symbol correlations
  • Regime-specific behaviours not yet modelled
  • Timing edges (time-of-day, session transitions)
  • Structural anomalies (unusual volume/price divergences)

Injectable — does NOT modify existing files.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from backend.services.trading.self_evolution_models import (
    DiscoveredEdge,
    DiscoveryReport,
    DiscoveryType,
)


class DiscoveryEngine:
    """Scans market data and trade outcomes to discover new edges."""

    # ── Configuration ─────────────────────────────────────────────
    MIN_SAMPLES = 15            # minimum data points for a discovery
    EDGE_THRESHOLD = 0.02       # 2 % minimum expected edge
    EVIDENCE_THRESHOLD = 0.55   # evidence strength above this → validated
    CORRELATION_WINDOW = 30     # bars for correlation analysis
    MAX_DISCOVERIES = 60        # cap stored discoveries

    def __init__(self) -> None:
        # symbol → list of (price, volume, ts, context)
        self._market_data: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        # Trade outcomes bucketed by various dimensions
        self._trade_outcomes: List[Dict[str, Any]] = []
        # Discovered edges
        self._edges: Dict[str, DiscoveredEdge] = {}
        self._last_report: DiscoveryReport | None = None

    # ── Ingest ────────────────────────────────────────────────────
    def ingest_market_data(
        self,
        symbol: str,
        price: float,
        volume: float,
        volatility: float = 0.0,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Feed market data for pattern discovery."""
        ctx = context or {}
        self._market_data[symbol].append({
            "price": price,
            "volume": volume,
            "volatility": volatility,
            "ts": time.time(),
            **ctx,
        })
        # Keep bounded
        if len(self._market_data[symbol]) > 500:
            self._market_data[symbol] = self._market_data[symbol][-500:]

    def ingest_trade_outcome(
        self,
        symbol: str,
        pnl_pct: float,
        won: bool,
        context: Dict[str, Any] | None = None,
    ) -> None:
        """Feed trade outcomes for edge discovery."""
        ctx = context or {}
        self._trade_outcomes.append({
            "symbol": symbol,
            "pnl_pct": pnl_pct,
            "won": won,
            "ts": time.time(),
            **ctx,
        })
        if len(self._trade_outcomes) > 1000:
            self._trade_outcomes = self._trade_outcomes[-1000:]

    # ── Discover ──────────────────────────────────────────────────
    def discover(self) -> DiscoveryReport:
        """Run all discovery algorithms and return new edges found."""
        new_edges: List[DiscoveredEdge] = []

        # 1. Emerging patterns (volume/price divergence)
        for edge in self._scan_emerging_patterns():
            self._edges[edge.edge_id] = edge
            new_edges.append(edge)

        # 2. New correlations between symbols
        for edge in self._scan_correlations():
            self._edges[edge.edge_id] = edge
            new_edges.append(edge)

        # 3. Regime-specific behaviours
        for edge in self._scan_regime_behaviors():
            self._edges[edge.edge_id] = edge
            new_edges.append(edge)

        # 4. Timing edges (session-based)
        for edge in self._scan_timing_edges():
            self._edges[edge.edge_id] = edge
            new_edges.append(edge)

        # 5. Structural anomalies
        for edge in self._scan_structural_anomalies():
            self._edges[edge.edge_id] = edge
            new_edges.append(edge)

        # Trim to max
        if len(self._edges) > self.MAX_DISCOVERIES:
            sorted_e = sorted(self._edges.values(),
                              key=lambda e: e.evidence_strength, reverse=True)
            self._edges = {e.edge_id: e for e in sorted_e[:self.MAX_DISCOVERIES]}

        all_edges = list(self._edges.values())
        validated = [e for e in all_edges if e.validated]

        report = DiscoveryReport(
            edges=new_edges,
            total_edges=len(all_edges),
            validated_count=len(validated),
            strongest_edge=max(all_edges, key=lambda e: e.evidence_strength).edge_id if all_edges else "",
        )
        self._last_report = report
        return report

    # ── Getters ───────────────────────────────────────────────────
    def get_last_report(self) -> DiscoveryReport | None:
        return self._last_report

    def get_edge(self, edge_id: str) -> Optional[DiscoveredEdge]:
        return self._edges.get(edge_id)

    def get_all_edges(self) -> List[DiscoveredEdge]:
        return list(self._edges.values())

    def get_validated_edges(self) -> List[DiscoveredEdge]:
        return [e for e in self._edges.values() if e.validated]

    # ── Scanner 1: Emerging Patterns ──────────────────────────────
    def _scan_emerging_patterns(self) -> List[DiscoveredEdge]:
        """Detect volume/price divergences and unusual patterns."""
        edges: List[DiscoveredEdge] = []

        for symbol, data in self._market_data.items():
            if len(data) < self.MIN_SAMPLES:
                continue

            recent = data[-self.MIN_SAMPLES:]
            prices = [d["price"] for d in recent]
            volumes = [d["volume"] for d in recent]

            if len(prices) < 3:
                continue

            # Price trend
            price_slope = self._slope(prices)
            vol_slope = self._slope(volumes)

            # Volume/price divergence: volume increasing while price flat/declining
            if vol_slope > 0.3 and price_slope < -0.1:
                strength = min(1.0, vol_slope * abs(price_slope))
                if strength > 0.2:
                    edge_id = self._make_id(f"emerging_vp_div_{symbol}")
                    edges.append(DiscoveredEdge(
                        edge_id=edge_id,
                        discovery_type=DiscoveryType.EMERGING_PATTERN,
                        description=f"Volume/price divergence on {symbol}: vol↑ price↓",
                        evidence_strength=strength,
                        sample_size=len(recent),
                        expected_edge_pct=strength * 0.05,
                        symbols_affected=[symbol],
                        conditions={"vol_slope": round(vol_slope, 4), "price_slope": round(price_slope, 4)},
                        validated=strength >= self.EVIDENCE_THRESHOLD,
                    ))

            # Volatility compression followed by expansion
            vols = [d.get("volatility", 0) for d in recent]
            if len(vols) >= 10 and all(v > 0 for v in vols):
                early_vol = sum(vols[:len(vols)//2]) / (len(vols)//2)
                late_vol = sum(vols[len(vols)//2:]) / (len(vols) - len(vols)//2)
                if early_vol > 0 and late_vol / early_vol > 2.0:
                    ratio = late_vol / early_vol
                    strength = min(1.0, (ratio - 2.0) / 2.0 + 0.5)
                    edge_id = self._make_id(f"emerging_vol_expand_{symbol}")
                    edges.append(DiscoveredEdge(
                        edge_id=edge_id,
                        discovery_type=DiscoveryType.EMERGING_PATTERN,
                        description=f"Volatility expansion on {symbol}: {ratio:.1f}x increase",
                        evidence_strength=strength,
                        sample_size=len(recent),
                        expected_edge_pct=strength * 0.03,
                        symbols_affected=[symbol],
                        conditions={"vol_ratio": round(ratio, 2)},
                        validated=strength >= self.EVIDENCE_THRESHOLD,
                    ))

        return edges

    # ── Scanner 2: Correlations ───────────────────────────────────
    def _scan_correlations(self) -> List[DiscoveredEdge]:
        """Detect new inter-symbol correlations."""
        edges: List[DiscoveredEdge] = []
        symbols = list(self._market_data.keys())

        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                s1, s2 = symbols[i], symbols[j]
                d1 = self._market_data[s1]
                d2 = self._market_data[s2]

                min_len = min(len(d1), len(d2), self.CORRELATION_WINDOW)
                if min_len < self.MIN_SAMPLES:
                    continue

                p1 = [d["price"] for d in d1[-min_len:]]
                p2 = [d["price"] for d in d2[-min_len:]]

                # Returns in pct
                r1 = [(p1[k] - p1[k-1]) / p1[k-1] if p1[k-1] != 0 else 0 for k in range(1, len(p1))]
                r2 = [(p2[k] - p2[k-1]) / p2[k-1] if p2[k-1] != 0 else 0 for k in range(1, len(p2))]

                corr = self._correlation(r1, r2)
                if abs(corr) > 0.70:
                    strength = min(1.0, (abs(corr) - 0.70) / 0.30 + 0.5)
                    direction = "positive" if corr > 0 else "negative"
                    edge_id = self._make_id(f"corr_{s1}_{s2}")
                    edges.append(DiscoveredEdge(
                        edge_id=edge_id,
                        discovery_type=DiscoveryType.NEW_CORRELATION,
                        description=f"{direction} correlation between {s1} and {s2}: {corr:.2f}",
                        evidence_strength=strength,
                        sample_size=min_len,
                        expected_edge_pct=abs(corr) * 0.02,
                        symbols_affected=[s1, s2],
                        conditions={"correlation": round(corr, 4)},
                        validated=strength >= self.EVIDENCE_THRESHOLD,
                    ))

        return edges

    # ── Scanner 3: Regime Behaviours ──────────────────────────────
    def _scan_regime_behaviors(self) -> List[DiscoveredEdge]:
        """Detect regime-specific trading edges."""
        edges: List[DiscoveredEdge] = []

        # Group trade outcomes by regime
        regime_buckets: Dict[str, List[Dict]] = defaultdict(list)
        for trade in self._trade_outcomes:
            regime = trade.get("regime", "unknown")
            regime_buckets[regime].append(trade)

        global_wr = (sum(1 for t in self._trade_outcomes if t.get("won", False))
                     / max(1, len(self._trade_outcomes)))

        for regime, trades in regime_buckets.items():
            if len(trades) < self.MIN_SAMPLES:
                continue

            wr = sum(1 for t in trades if t.get("won", False)) / len(trades)
            avg_pnl = sum(t.get("pnl_pct", 0) for t in trades) / len(trades)
            wr_diff = wr - global_wr

            if abs(wr_diff) > 0.10:  # significant deviation
                strength = min(1.0, abs(wr_diff) / 0.30) * min(1.0, len(trades) / (self.MIN_SAMPLES * 2))
                direction = "outperform" if wr_diff > 0 else "underperform"
                edge_id = self._make_id(f"regime_{regime}")
                edges.append(DiscoveredEdge(
                    edge_id=edge_id,
                    discovery_type=DiscoveryType.REGIME_BEHAVIOR,
                    description=f"Trades {direction} in {regime} regime: wr_diff={wr_diff:+.2%}",
                    evidence_strength=strength,
                    sample_size=len(trades),
                    expected_edge_pct=abs(wr_diff) * abs(avg_pnl),
                    symbols_affected=list(set(t.get("symbol", "") for t in trades)),
                    conditions={"regime": regime, "wr_diff": round(wr_diff, 4)},
                    validated=strength >= self.EVIDENCE_THRESHOLD,
                ))

        return edges

    # ── Scanner 4: Timing Edges ───────────────────────────────────
    def _scan_timing_edges(self) -> List[DiscoveredEdge]:
        """Detect time-based edges (session, hour-of-day)."""
        edges: List[DiscoveredEdge] = []

        session_buckets: Dict[str, List[Dict]] = defaultdict(list)
        for trade in self._trade_outcomes:
            session = trade.get("session", "unknown")
            session_buckets[session].append(trade)

        global_wr = (sum(1 for t in self._trade_outcomes if t.get("won", False))
                     / max(1, len(self._trade_outcomes)))

        for session, trades in session_buckets.items():
            if len(trades) < self.MIN_SAMPLES:
                continue

            wr = sum(1 for t in trades if t.get("won", False)) / len(trades)
            wr_diff = wr - global_wr

            if abs(wr_diff) > 0.08:
                strength = min(1.0, abs(wr_diff) / 0.25) * min(1.0, len(trades) / (self.MIN_SAMPLES * 2))
                edge_id = self._make_id(f"timing_{session}")
                edges.append(DiscoveredEdge(
                    edge_id=edge_id,
                    discovery_type=DiscoveryType.TIMING_EDGE,
                    description=f"Session {session} edge: wr_diff={wr_diff:+.2%}",
                    evidence_strength=strength,
                    sample_size=len(trades),
                    expected_edge_pct=abs(wr_diff) * 0.03,
                    symbols_affected=list(set(t.get("symbol", "") for t in trades)),
                    conditions={"session": session, "wr_diff": round(wr_diff, 4)},
                    validated=strength >= self.EVIDENCE_THRESHOLD,
                ))

        return edges

    # ── Scanner 5: Structural Anomalies ───────────────────────────
    def _scan_structural_anomalies(self) -> List[DiscoveredEdge]:
        """Detect unusual structural patterns (volume spikes, gap behaviours)."""
        edges: List[DiscoveredEdge] = []

        for symbol, data in self._market_data.items():
            if len(data) < self.MIN_SAMPLES:
                continue

            volumes = [d["volume"] for d in data[-self.CORRELATION_WINDOW:]]
            if len(volumes) < 10:
                continue

            avg_vol = sum(volumes) / len(volumes)
            if avg_vol == 0:
                continue

            # Count extreme volume spikes
            spikes = sum(1 for v in volumes if v > avg_vol * 3.0)
            if spikes >= 3:
                strength = min(1.0, spikes / 8.0 + 0.3)
                edge_id = self._make_id(f"anomaly_vol_spike_{symbol}")
                edges.append(DiscoveredEdge(
                    edge_id=edge_id,
                    discovery_type=DiscoveryType.STRUCTURAL_ANOMALY,
                    description=f"Recurring volume spikes on {symbol}: {spikes} in {len(volumes)} bars",
                    evidence_strength=strength,
                    sample_size=len(volumes),
                    expected_edge_pct=strength * 0.04,
                    symbols_affected=[symbol],
                    conditions={"spikes": spikes, "avg_volume": round(avg_vol, 2)},
                    validated=strength >= self.EVIDENCE_THRESHOLD,
                ))

        return edges

    # ── Helpers ───────────────────────────────────────────────────
    @staticmethod
    def _slope(values: List[float]) -> float:
        """Normalised slope of a series."""
        n = len(values)
        if n < 2:
            return 0.0
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n
        num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        den = sum((i - x_mean) ** 2 for i in range(n))
        if den == 0:
            return 0.0
        raw = num / den
        std = math.sqrt(sum((v - y_mean) ** 2 for v in values) / n) if n > 0 else 1.0
        if std == 0:
            return 0.0
        return max(-1.0, min(1.0, raw / std))

    @staticmethod
    def _correlation(x: List[float], y: List[float]) -> float:
        """Pearson correlation coefficient."""
        n = min(len(x), len(y))
        if n < 3:
            return 0.0
        x, y = x[:n], y[:n]
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
        dx = math.sqrt(sum((xi - mx) ** 2 for xi in x))
        dy = math.sqrt(sum((yi - my) ** 2 for yi in y))
        if dx == 0 or dy == 0:
            return 0.0
        return num / (dx * dy)

    @staticmethod
    def _make_id(key: str) -> str:
        return "disc_" + hashlib.md5(key.encode()).hexdigest()[:12]
