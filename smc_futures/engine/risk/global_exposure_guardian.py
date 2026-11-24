"""Global Exposure Guardian
=========================

A last-line-of-defense guard that rejects trading intents which would push the
system beyond configured exposure limits (total, net, per-symbol, per-group,
per-broker).

This module is intentionally conservative and runs *before* execution.

The guardian does not rely on a single source of truth (exchange vs DB). Instead
it can be fed with a list of open positions from multiple sources (orchestrators,
reconciler, persisted positions, etc.).

It is designed to be lightweight and easy to integrate into the existing
SMC pipeline.

Usage:
    guard = GlobalExposureGuardian()
    result = guard.check_signal(
        symbol="BTCUSDT",
        side="long",
        notional_usd=1234.0,
        open_positions=current_open_positions,
    )

Return value:
    {
        "allowed": bool,
        "reason": str,
        "current_total_usd": float,
        "projected_total_usd": float,
        "limit_usd": float,
        "details": {...}
    }
"""

import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ExposurePosition:
    symbol: str
    side: str  # "long" or "short"
    notional_usd: float
    broker: Optional[str] = None
    group: Optional[str] = None


@dataclass
class ExposureLimits:
    max_total_usd: float = float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "1000000"))
    max_net_usd: float = float(os.getenv("MAX_NET_EXPOSURE_USD", "500000"))
    max_symbol_usd: float = float(os.getenv("MAX_SYMBOL_EXPOSURE_USD", "250000"))
    max_group_usd: float = float(os.getenv("MAX_GROUP_EXPOSURE_USD", "250000"))
    max_broker_usd: float = float(os.getenv("MAX_BROKER_EXPOSURE_USD", "500000"))

    # Optional throttle thresholds (0-1). If projected exposure exceeds this fraction of the limit,
    # the guard will return a throttle advisory rather than a hard block.
    throttle_pct: float = float(os.getenv("EXPOSURE_GUARD_THROTTLE_PCT", "0.85"))

    # Comma-separated symbol=>group list, e.g. "BTCUSDT:crypto_beta,ETHUSDT:crypto_beta"
    groups_map_raw: str = field(default_factory=lambda: os.getenv("EXPOSURE_GROUPS", ""))

    def parse_groups(self) -> Dict[str, str]:
        """Parse env var into {symbol: group} mapping."""
        mapping: Dict[str, str] = {}
        raw = (self.groups_map_raw or "").strip()
        if not raw:
            return mapping
        for pair in raw.split("\n"):
            pair = pair.strip()
            if not pair:
                continue
            if ":" in pair:
                sym, grp = [p.strip() for p in pair.split(":", 1)]
                if sym and grp:
                    mapping[sym.upper()] = grp
        return mapping


class GlobalExposureGuardian:
    """Global exposure guard for total, net, per-symbol and per-group limits."""

    def __init__(
        self,
        limits: Optional[ExposureLimits] = None,
    ):
        self.limits = limits or ExposureLimits()
        self.groups_map = self.limits.parse_groups()

    def _normalize_side(self, side: str) -> str:
        return "long" if str(side).lower().startswith("l") else "short"

    def _position_key(self, symbol: str, side: str) -> str:
        return f"{symbol.upper()}_{self._normalize_side(side)}"

    def _aggregate_exposure(
        self,
        positions: List[ExposurePosition],
    ) -> Dict[str, Any]:
        """Aggregate exposure stats from a list of positions."""
        total_gross = 0.0
        net = 0.0
        long_usd = 0.0
        short_usd = 0.0
        by_symbol: Dict[str, float] = {}
        by_group: Dict[str, float] = {}
        by_broker: Dict[str, float] = {}

        for pos in positions:
            notional = abs(pos.notional_usd)
            total_gross += notional
            side = self._normalize_side(pos.side)
            if side == "long":
                long_usd += notional
                net += notional
            else:
                short_usd += notional
                net -= notional

            sym = pos.symbol.upper()
            by_symbol[sym] = by_symbol.get(sym, 0.0) + notional

            broker = pos.broker or "unknown"
            by_broker[broker] = by_broker.get(broker, 0.0) + notional

            group = pos.group or self.groups_map.get(sym)
            if group:
                by_group[group] = by_group.get(group, 0.0) + notional

        return {
            "total_gross": total_gross,
            "net": net,
            "long_usd": long_usd,
            "short_usd": short_usd,
            "by_symbol": by_symbol,
            "by_group": by_group,
            "by_broker": by_broker,
        }

    def check_signal(
        self,
        symbol: str,
        side: str,
        notional_usd: float,
        open_positions: List[ExposurePosition],
        pending_positions: Optional[List[ExposurePosition]] = None,
        broker: Optional[str] = None,
        group: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Evaluate a trading intent against exposure limits.

        Returns a dict containing:
            allowed: bool
            reason: str
            current_total_usd: float
            projected_total_usd: float
            limit_usd: float
            details: dict
        """
        symbol = symbol.upper()
        side = self._normalize_side(side)
        notional_usd = abs(notional_usd)

        # Include pending orders in exposure snapshot if provided
        combined_positions = list(open_positions)
        if pending_positions:
            combined_positions.extend(pending_positions)

        base_snapshot = self._aggregate_exposure(combined_positions)

        projected_total = base_snapshot["total_gross"] + notional_usd
        projected_net = abs(base_snapshot["net"] + (notional_usd if side == "long" else -notional_usd))

        projected_by_symbol = dict(base_snapshot["by_symbol"])
        projected_by_symbol[symbol] = projected_by_symbol.get(symbol, 0.0) + notional_usd

        projected_by_group = dict(base_snapshot["by_group"])
        if group:
            projected_by_group[group] = projected_by_group.get(group, 0.0) + notional_usd
        else:
            grp = self.groups_map.get(symbol)
            if grp:
                projected_by_group[grp] = projected_by_group.get(grp, 0.0) + notional_usd

        projected_by_broker = dict(base_snapshot["by_broker"])
        if broker:
            projected_by_broker[broker] = projected_by_broker.get(broker, 0.0) + notional_usd

        # Build response base
        resp = {
            "allowed": True,
            "reason": "OK",
            "current_total_usd": base_snapshot["total_gross"],
            "projected_total_usd": projected_total,
            "limit_usd": self.limits.max_total_usd,
            "details": {
                "long_usd": base_snapshot.get("long_usd", 0.0),
                "short_usd": base_snapshot.get("short_usd", 0.0),
                "net_usd": base_snapshot["net"],
                "projected_net_usd": projected_net,
                "by_symbol": base_snapshot["by_symbol"],
                "by_group": base_snapshot["by_group"],
                "by_broker": base_snapshot["by_broker"],
                "pending_usd": sum(p.notional_usd for p in (pending_positions or [])),
            },
        }

        # Total exposure check
        throttle_pct = max(0.0, min(1.0, self.limits.throttle_pct))
        throttle_trigger = self.limits.max_total_usd * throttle_pct if self.limits.max_total_usd > 0 else 0.0

        if self.limits.max_total_usd > 0 and projected_total > self.limits.max_total_usd:
            resp.update({
                "allowed": False,
                "reason": "MAX_TOTAL_EXPOSURE",
                "limit_usd": self.limits.max_total_usd,
            })
            return resp

        if throttle_trigger > 0 and projected_total > throttle_trigger:
            resp.update({
                "allowed": True,
                "reason": "THROTTLE_TOTAL_EXPOSURE",
                "throttle_pct": throttle_pct,
                "throttle_level": projected_total / self.limits.max_total_usd if self.limits.max_total_usd > 0 else 0.0,
            })

        # Net directional exposure
        if self.limits.max_net_usd > 0 and projected_net > self.limits.max_net_usd:
            resp.update({
                "allowed": False,
                "reason": "MAX_NET_EXPOSURE",
                "limit_usd": self.limits.max_net_usd,
            })
            return resp

        # Per-symbol exposure
        sym_exposure = projected_by_symbol.get(symbol, 0.0)
        if self.limits.max_symbol_usd > 0 and sym_exposure > self.limits.max_symbol_usd:
            resp.update({
                "allowed": False,
                "reason": "MAX_SYMBOL_EXPOSURE",
                "limit_usd": self.limits.max_symbol_usd,
            })
            return resp

        # Per-group exposure
        # Use explicit group if provided, otherwise infer from symbol mapping.
        group_to_check = group or self.groups_map.get(symbol)
        if group_to_check:
            grp_exposure = projected_by_group.get(group_to_check, 0.0)
            if self.limits.max_group_usd > 0 and grp_exposure > self.limits.max_group_usd:
                resp.update({
                    "allowed": False,
                    "reason": "MAX_GROUP_EXPOSURE",
                    "limit_usd": self.limits.max_group_usd,
                })
                return resp

            # Throttle on group if configured
            throttle_group_trigger = self.limits.max_group_usd * throttle_pct if self.limits.max_group_usd > 0 else 0.0
            if throttle_group_trigger > 0 and grp_exposure > throttle_group_trigger:
                resp.update({
                    "allowed": True,
                    "reason": "THROTTLE_GROUP_EXPOSURE",
                    "throttle_pct": throttle_pct,
                    "throttle_level": grp_exposure / self.limits.max_group_usd if self.limits.max_group_usd > 0 else 0.0,
                })

        # Broker exposure
        if broker:
            broker_exposure = projected_by_broker.get(broker, 0.0)
            if self.limits.max_broker_usd > 0 and broker_exposure > self.limits.max_broker_usd:
                resp.update({
                    "allowed": False,
                    "reason": "MAX_BROKER_EXPOSURE",
                    "limit_usd": self.limits.max_broker_usd,
                })
                return resp

            # Throttle on broker if configured
            throttle_broker_trigger = self.limits.max_broker_usd * throttle_pct if self.limits.max_broker_usd > 0 else 0.0
            if throttle_broker_trigger > 0 and broker_exposure > throttle_broker_trigger:
                resp.update({
                    "allowed": True,
                    "reason": "THROTTLE_BROKER_EXPOSURE",
                    "throttle_pct": throttle_pct,
                    "throttle_level": broker_exposure / self.limits.max_broker_usd if self.limits.max_broker_usd > 0 else 0.0,
                })

        return resp
