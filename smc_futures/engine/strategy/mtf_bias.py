"""
Multi-Timeframe Bias Engine — 3-Tier Hierarchy
═══════════════════════════════════════════════
  HTF (4H)  → CONTEXT / FILTER  (bias: BULLISH | BEARISH | NEUTRAL)
  MTF (15m) → SETUP  / ZONE     (POI: Order Blocks + FVG)
  LTF (5m)  → EXECUTION / TRIGGER (CHoCH / BOS / liquidity sweep)

Rules
─────
• HTF dominates: if bearish, LONG is blocked (unless ALLOW_COUNTERTREND).
• HTF NEUTRAL due to < MIN_HTF_CANDLES → trading disabled in prod.
• debug_permissive mode → allows but halves risk, requires CHoCH+FVG.
"""

from typing import Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import logging

from smc_futures.engine.utils.reason_codes import ReasonCode

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────
#  Data classes
# ──────────────────────────────────────────────────

class Bias(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass
class TimeframeAnalysis:
    """State snapshot for one timeframe."""
    timeframe: str
    bias: Bias = Bias.NEUTRAL
    strength: float = 0.0
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    bos_count: int = 0
    choch_count: int = 0
    is_uptrend: Optional[bool] = None
    order_blocks: List[Dict] = field(default_factory=list)
    fvgs: List[Dict] = field(default_factory=list)
    candle_count: int = 0


# ──────────────────────────────────────────────────
#  Engine
# ──────────────────────────────────────────────────

class MTFBiasEngine:
    """
    3-Tier Multi-Timeframe Bias Detection
    ──────────────────────────────────────
    Tier 1 — HTF (4H) : directional bias (update_htf_context)
    Tier 2 — MTF (15m): POI setup zones  (update_mtf_setup)
    Tier 3 — LTF (5m) : entry trigger    (check_ltf_trigger)
    """

    def __init__(
        self,
        min_htf_candles: int = 300,
        mode: str = "prod",
        allow_countertrend: bool = False,
    ):
        self.min_htf_candles = min_htf_candles
        self.mode = mode                          # "prod" | "debug_permissive"
        self.allow_countertrend = allow_countertrend

        # ── Tier states ─────────────────────────
        self.htf = TimeframeAnalysis(timeframe="4H")
        self.mtf = TimeframeAnalysis(timeframe="15M")
        self.ltf = TimeframeAnalysis(timeframe="5M")

        # ── Derived ─────────────────────────────
        self.current_bias = Bias.NEUTRAL
        self.bias_strength: float = 0.0
        self.trading_disabled: bool = False
        self.rejection_reason: Optional[ReasonCode] = None

    # ──────────────────────────────────────────
    #  TIER 1 — HTF Context (4H)
    # ──────────────────────────────────────────

    def update_htf_context(self, structure_state: Dict) -> None:
        """
        Feed 4H StructureEngine output.
        Sets bias = BULLISH / BEARISH / NEUTRAL.
        Enforces the candle gate.
        """
        candle_count = structure_state.get("candle_count", 0)
        self.htf.candle_count = candle_count
        self.htf.last_swing_high = structure_state.get("last_swing_high")
        self.htf.last_swing_low = structure_state.get("last_swing_low")
        self.htf.bos_count = structure_state.get("recent_bos", 0)
        self.htf.choch_count = structure_state.get("recent_choch", 0)
        self.htf.is_uptrend = structure_state.get("is_uptrend")

        # ── Candle gate ──────────────────────────
        if candle_count < self.min_htf_candles:
            self.htf.bias = Bias.NEUTRAL
            self.htf.strength = 0.0
            self.rejection_reason = ReasonCode.HTF_INSUFFICIENT_DATA

            if self.mode == "prod":
                self.trading_disabled = True
            else:
                self.trading_disabled = False  # debug_permissive

            logger.info(
                f"[MTF] {self.htf.timeframe}"
                f" | bias=NEUTRAL"
                f" | candles={candle_count} < {self.min_htf_candles}"
                f" | trading_disabled={self.trading_disabled}"
                f" | reason={ReasonCode.HTF_INSUFFICIENT_DATA}"
            )
            self._recalc()
            return

        # ── Trend classification ─────────────────
        str_strength = structure_state.get("structure_strength", 0.5)

        if structure_state.get("is_uptrend"):
            self.htf.bias = Bias.BULLISH
            self.htf.strength = min(1.0, 0.5 + str_strength * 0.5)
        else:
            self.htf.bias = Bias.BEARISH
            self.htf.strength = min(1.0, 0.5 + str_strength * 0.5)

        self.trading_disabled = False
        self.rejection_reason = None

        logger.info(
            f"[MTF] {self.htf.timeframe}"
            f" | bias={self.htf.bias.value.upper()}"
            f" | strength={self.htf.strength:.2f}"
            f" | candles={candle_count}"
        )
        self._recalc()

    # ──────────────────────────────────────────
    #  TIER 2 — MTF Setup (15m)
    # ──────────────────────────────────────────

    def update_mtf_setup(
        self,
        structure_state: Dict,
        ob_state: Dict,
        fvg_state: Dict,
    ) -> None:
        """
        Feed 15m engines output.
        Detects POI zones (OB + FVG) aligned with HTF bias.
        """
        self.mtf.bos_count = structure_state.get("recent_bos", 0)
        self.mtf.choch_count = structure_state.get("recent_choch", 0)
        self.mtf.last_swing_high = structure_state.get("last_swing_high")
        self.mtf.last_swing_low = structure_state.get("last_swing_low")
        self.mtf.is_uptrend = structure_state.get("is_uptrend")
        self.mtf.strength = min(
            1.0,
            0.5 + structure_state.get("structure_strength", 0.5) * 0.5,
        )

        # Store raw OB/FVG for signal builder
        self.mtf.order_blocks = ob_state.get("blocks", [])
        self.mtf.fvgs = fvg_state.get("fvgs", [])

        # Determine MTF-level bias from structure
        if structure_state.get("is_uptrend"):
            self.mtf.bias = Bias.BULLISH
        elif structure_state.get("is_uptrend") is False:
            self.mtf.bias = Bias.BEARISH
        else:
            self.mtf.bias = Bias.NEUTRAL

    # ──────────────────────────────────────────
    #  TIER 3 — LTF Trigger (5m)
    # ──────────────────────────────────────────

    def update_ltf_data(self, structure_state: Dict) -> None:
        """Feed 5m StructureEngine output (or 15m fallback)."""
        self.ltf.bos_count = structure_state.get("recent_bos", 0)
        self.ltf.choch_count = structure_state.get("recent_choch", 0)
        self.ltf.last_swing_high = structure_state.get("last_swing_high")
        self.ltf.last_swing_low = structure_state.get("last_swing_low")
        self.ltf.is_uptrend = structure_state.get("is_uptrend")
        self.ltf.strength = min(
            1.0,
            0.5 + structure_state.get("structure_strength", 0.5) * 0.5,
        )

        if structure_state.get("is_uptrend"):
            self.ltf.bias = Bias.BULLISH
        elif structure_state.get("is_uptrend") is False:
            self.ltf.bias = Bias.BEARISH
        else:
            self.ltf.bias = Bias.NEUTRAL

    def check_ltf_trigger(self, symbol: str) -> Dict:
        """
        Evaluate if LTF (5m) provides an execution trigger:
          - CHoCH in bias direction
          - BOS in bias direction
          - Liquidity sweep + flip

        Returns dict with:
            confirmed: bool
            trigger_type: str | None   ("choch" | "bos" | "sweep_flip")
            direction: str | None      ("long" | "short")
            reason: ReasonCode | None
        """
        bias = self.current_bias

        # ── Trading disabled gate ────────────────
        if self.trading_disabled:
            return {
                "confirmed": False,
                "trigger_type": None,
                "direction": None,
                "reason": ReasonCode.HTF_INSUFFICIENT_DATA,
            }

        # ── NEUTRAL gate ─────────────────────────
        if bias == Bias.NEUTRAL:
            if self.mode != "debug_permissive":
                return {
                    "confirmed": False,
                    "trigger_type": None,
                    "direction": None,
                    "reason": ReasonCode.HTF_NEUTRAL_PROD,
                }
            # debug_permissive: fallback to LTF structure
            bias = self.ltf.bias
            if bias == Bias.NEUTRAL:
                return {
                    "confirmed": False,
                    "trigger_type": None,
                    "direction": None,
                    "reason": ReasonCode.HTF_NEUTRAL,
                }

        # ── Determine expected direction ─────────
        expected_dir = "long" if bias == Bias.BULLISH else "short"

        # ── Counter-trend guard ──────────────────
        if self.ltf.is_uptrend is None:
            # Structure not yet determined — skip, don't false-reject
            return {
                "confirmed": False,
                "trigger_type": None,
                "direction": None,
                "reason": ReasonCode.LTF_NO_CONFIRMATION,
            }
        ltf_dir = "long" if self.ltf.is_uptrend else "short"
        if ltf_dir != expected_dir and not self.allow_countertrend:
            logger.info(
                f"[FILTER] {symbol} | REJECTED"
                f" | reason={ReasonCode.COUNTERTREND_BLOCKED}"
                f" | htf_bias={bias.value.upper()}"
                f" | ltf_dir={ltf_dir}"
            )
            return {
                "confirmed": False,
                "trigger_type": None,
                "direction": None,
                "reason": ReasonCode.COUNTERTREND_BLOCKED,
            }

        # ── Check for CHoCH trigger ──────────────
        if self.ltf.choch_count > 0:
            if bias == Bias.BULLISH and self.ltf.is_uptrend:
                return {"confirmed": True, "trigger_type": "choch", "direction": "long", "reason": None}
            if bias == Bias.BEARISH and not self.ltf.is_uptrend:
                return {"confirmed": True, "trigger_type": "choch", "direction": "short", "reason": None}

        # ── Check for BOS trigger ────────────────
        if self.ltf.bos_count > 0:
            if bias == Bias.BULLISH and self.ltf.is_uptrend:
                return {"confirmed": True, "trigger_type": "bos", "direction": "long", "reason": None}
            if bias == Bias.BEARISH and not self.ltf.is_uptrend:
                return {"confirmed": True, "trigger_type": "bos", "direction": "short", "reason": None}

        # ── Countertrend triggers (when allowed) ─
        if self.allow_countertrend:
            if self.ltf.choch_count > 0:
                if bias == Bias.BEARISH and self.ltf.is_uptrend:
                    return {"confirmed": True, "trigger_type": "choch", "direction": "long", "reason": None}
                if bias == Bias.BULLISH and not self.ltf.is_uptrend:
                    return {"confirmed": True, "trigger_type": "choch", "direction": "short", "reason": None}
            if self.ltf.bos_count > 0:
                if bias == Bias.BEARISH and self.ltf.is_uptrend:
                    return {"confirmed": True, "trigger_type": "bos", "direction": "long", "reason": None}
                if bias == Bias.BULLISH and not self.ltf.is_uptrend:
                    return {"confirmed": True, "trigger_type": "bos", "direction": "short", "reason": None}

        # ── No trigger ───────────────────────────
        return {
            "confirmed": False,
            "trigger_type": None,
            "direction": None,
            "reason": ReasonCode.NO_LTF_TRIGGER,
        }

    # ──────────────────────────────────────────
    #  Helpers — kept for legacy compatibility
    # ──────────────────────────────────────────

    def update_htf_data(self, structure_state: Dict) -> None:
        """Legacy alias → update_htf_context."""
        self.update_htf_context(structure_state)

    def update_ob_data(self, timeframe: str, ob_state: Dict) -> None:
        """Store OB data for a timeframe (used by old orchestrator)."""
        if timeframe.upper() in ("15M", "15MIN"):
            self.mtf.order_blocks = ob_state.get("blocks", [])

    def update_fvg_data(self, timeframe: str, fvg_state: Dict) -> None:
        """Store FVG data for a timeframe (used by old orchestrator)."""
        if timeframe.upper() in ("15M", "15MIN"):
            self.mtf.fvgs = fvg_state.get("fvgs", [])

    def calculate_bias(self) -> Bias:
        """Recalculate and return bias (kept for backward compat)."""
        self._recalc()
        return self.current_bias

    def validate_ltf_entry(self, ltf_signal: Dict) -> bool:
        """Check if LTF signal direction aligns with HTF bias."""
        self._recalc()
        direction = ltf_signal.get("direction")
        if self.current_bias == Bias.BULLISH:
            return direction == "long"
        if self.current_bias == Bias.BEARISH:
            return direction == "short"
        return False

    def check_ltf_confirmation(self, ltf_structure: Dict) -> bool:
        """Verify LTF has BOS in bias direction."""
        self._recalc()
        if self.current_bias == Bias.BULLISH:
            return ltf_structure.get("recent_bos", 0) > 0 and ltf_structure.get("is_uptrend")
        if self.current_bias == Bias.BEARISH:
            return ltf_structure.get("recent_bos", 0) > 0 and not ltf_structure.get("is_uptrend")
        return False

    def find_entry_zones(self) -> List[Dict]:
        """Return POI zones from MTF (15m) aligned with HTF bias."""
        zones: List[Dict] = []
        bias = self.current_bias

        if bias == Bias.BULLISH:
            for ob in self.mtf.order_blocks:
                if ob.get("type") == "bullish":
                    zones.append({
                        "type": "order_block", "level": ob.get("low"),
                        "direction": "long", "timeframe": "15M",
                        "confidence": ob.get("confidence", 0.5),
                    })
            for fvg in self.mtf.fvgs:
                if fvg.get("type") == "bullish":
                    zones.append({
                        "type": "fvg", "level": fvg.get("bottom"),
                        "direction": "long", "timeframe": "15M",
                        "confidence": 0.7,
                    })

        elif bias == Bias.BEARISH:
            for ob in self.mtf.order_blocks:
                if ob.get("type") == "bearish":
                    zones.append({
                        "type": "order_block", "level": ob.get("high"),
                        "direction": "short", "timeframe": "15M",
                        "confidence": ob.get("confidence", 0.5),
                    })
            for fvg in self.mtf.fvgs:
                if fvg.get("type") == "bearish":
                    zones.append({
                        "type": "fvg", "level": fvg.get("top"),
                        "direction": "short", "timeframe": "15M",
                        "confidence": 0.7,
                    })

        return zones

    def get_current_state(self) -> Dict:
        """Export full MTF state for dashboard."""
        self._recalc()
        # Derive market regime from bias strength & trading disabled flag
        regime = self._derive_regime()
        return {
            "bias": self.current_bias.value,
            "bias_strength": self.bias_strength,
            "trading_disabled": self.trading_disabled,
            "regime": regime,
            "htf_4h": {
                "bias": self.htf.bias.value,
                "strength": self.htf.strength,
                "swing_high": self.htf.last_swing_high,
                "swing_low": self.htf.last_swing_low,
                "candle_count": self.htf.candle_count,
            },
            "mtf_15m": {
                "bias": self.mtf.bias.value,
                "strength": self.mtf.strength,
                "ob_count": len(self.mtf.order_blocks),
                "fvg_count": len(self.mtf.fvgs),
            },
            "ltf_5m": {
                "bias": self.ltf.bias.value,
                "bos_count": self.ltf.bos_count,
                "choch_count": self.ltf.choch_count,
            },
            "entry_zones": self.find_entry_zones(),
            # Legacy compat keys
            "htf_4h_legacy": {
                "bias": self.htf.bias.value,
                "strength": self.htf.strength,
                "swing_high": self.htf.last_swing_high,
                "swing_low": self.htf.last_swing_low,
            },
            "htf_1h": {
                "bias": "neutral",
                "strength": 0.5,
            },
        }

    # ──────────────────────────────────────────
    #  Internal
    # ──────────────────────────────────────────

    def _derive_regime(self) -> str:
        """Derive market regime from bias strength and trading state."""
        if self.trading_disabled:
            return "CHOP"
        strength = self.bias_strength
        if strength >= 0.6:
            return "TREND"
        elif strength >= 0.3:
            return "RANGE"
        else:
            return "CHOP"

    def _recalc(self) -> None:
        """Derive current_bias and bias_strength from HTF state."""
        self.current_bias = self.htf.bias
        self.bias_strength = self.htf.strength
