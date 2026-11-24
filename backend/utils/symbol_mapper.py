"""
Symbol Mapper — Cross-Broker Symbol Translation
═══════════════════════════════════════════════

Handles symbol conversion between different broker formats:
  - Bitget: BTCUSDT, ETHUSDT (no separator)
  - Kraken Spot: BTC/USD, ETH/USD (slash separator)
  - Kraken Futures: PF_XBTUSD (perpetual), PI_XBTUSD (inverse)

Kraken Futures Symbol Format:
  - PF_ prefix = Perpetual Futures (linear, USD-margined)
  - PI_ prefix = Perpetual Inverse (coin-margined)
  - FI_ prefix = Fixed-maturity Inverse
  - FF_ prefix = Fixed-maturity Futures

Supported mappings:
  BTCUSDT <-> PF_XBTUSD (Kraken uses XBT for Bitcoin)
  ETHUSDT <-> PF_ETHUSD
  SOLUSDT <-> PF_SOLUSD
  etc.

Usage:
    from backend.utils.symbol_mapper import SymbolMapper, BrokerType
    
    mapper = SymbolMapper()
    
    # Bitget -> Kraken
    kraken_sym = mapper.to_kraken("BTCUSDT")  # "PF_XBTUSD"
    
    # Kraken -> Bitget
    bitget_sym = mapper.to_bitget("PF_ETHUSD")  # "ETHUSDT"
    
    # Normalize for any broker
    symbol = mapper.normalize("BTCUSDT", BrokerType.KRAKEN)  # "PF_XBTUSD"
"""

import json
import logging
import os
import re
from enum import Enum
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class BrokerType(str, Enum):
    """Supported broker types."""
    BITGET = "bitget"
    KRAKEN = "kraken"


class KrakenProductType(str, Enum):
    """Kraken Futures product types."""
    PERPETUAL = "perpetual"      # PF_ prefix (linear, USD-margined)
    INVERSE = "inverse"          # PI_ prefix (coin-margined)
    FIXED_FUTURES = "fixed"      # FF_ prefix
    FIXED_INVERSE = "fixed_inv"  # FI_ prefix


# ══════════════════════════════════════════════════════════════════════════════
# SYMBOL MAPPINGS
# ══════════════════════════════════════════════════════════════════════════════

# Bitget symbol -> Kraken Futures symbol (perpetual)
# Note: Kraken uses XBT instead of BTC
BITGET_TO_KRAKEN_MAP: Dict[str, str] = {
    "BTCUSDT": "PF_XBTUSD",
    "ETHUSDT": "PF_ETHUSD",
    "SOLUSDT": "PF_SOLUSD",
    "XRPUSDT": "PF_XRPUSD",
    "AVAXUSDT": "PF_AVAXUSD",
    "LINKUSDT": "PF_LINKUSD",
    "DOTUSDT": "PF_DOTUSD",
    "ADAUSDT": "PF_ADAUSD",
    "MATICUSDT": "PF_MATICUSD",
    "LTCUSDT": "PF_LTCUSD",
    "BCHUSDT": "PF_BCHUSD",
    "ATOMUSDT": "PF_ATOMUSD",
    "UNIUSDT": "PF_UNIUSD",
    "NEARUSDT": "PF_NEARUSD",
    "APTUSDT": "PF_APTUSD",
    "ARBUSDT": "PF_ARBUSD",
    "OPUSDT": "PF_OPUSD",
    "FILUSDT": "PF_FILUSD",
    "TRXUSDT": "PF_TRXUSD",
    "SHIBUSDT": "PF_SHIBUSD",
    "DOGEUSDT": "PF_DOGEUSD",
    "PEPEUSDT": "PF_PEPEUSD",
}


def load_kraken_symbol_map_from_env() -> Dict[str, str]:
    """
    Load custom Kraken symbol mappings from KRAKEN_SYMBOL_MAP env var.
    
    Format (JSON):
        KRAKEN_SYMBOL_MAP={"BTCUSDT":"PI_XBTUSD","ETHUSDT":"PF_ETHUSD","SOLUSDT":"PF_SOLUSD"}
    
    Mappings from env override/extend the built-in BITGET_TO_KRAKEN_MAP.
    
    Returns:
        Dict of custom mappings from env, or empty dict if not set/invalid.
    """
    env_val = os.getenv("KRAKEN_SYMBOL_MAP", "").strip()
    if not env_val:
        return {}
    
    try:
        custom_map = json.loads(env_val)
        if not isinstance(custom_map, dict):
            logger.warning("[SYMBOL_MAPPER] KRAKEN_SYMBOL_MAP is not a valid dict, ignoring")
            return {}
        
        # Normalize keys to uppercase
        normalized = {k.upper(): v for k, v in custom_map.items()}
        logger.info("[SYMBOL_MAPPER] Loaded %d custom mappings from KRAKEN_SYMBOL_MAP", len(normalized))
        return normalized
    except json.JSONDecodeError as e:
        logger.error("[SYMBOL_MAPPER] Failed to parse KRAKEN_SYMBOL_MAP: %s", e)
        return {}


# Reverse mapping: Kraken -> Bitget
KRAKEN_TO_BITGET_MAP: Dict[str, str] = {v: k for k, v in BITGET_TO_KRAKEN_MAP.items()}

# Kraken spot format (for REST API OHLC)
BITGET_TO_KRAKEN_SPOT_MAP: Dict[str, str] = {
    "BTCUSDT": "XXBTZUSD",   # Kraken spot uses full ISO codes
    "ETHUSDT": "XETHZUSD",
    "SOLUSDT": "SOLUSD",
    "XRPUSDT": "XXRPZUSD",
    "AVAXUSDT": "AVAXUSD",
    "LINKUSDT": "LINKUSD",
    "DOTUSDT": "DOTUSD",
    "ADAUSDT": "ADAUSD",
    "MATICUSDT": "MATICUSD",
    "LTCUSDT": "XLTCZUSD",
    "BCHUSDT": "BCHUSD",
    "ATOMUSDT": "ATOMUSD",
    "UNIUSDT": "UNIUSD",
    "NEARUSDT": "NEARUSD",
    "APTUSDT": "APTUSD",
    "ARBUSDT": "ARBUSD",
    "OPUSDT": "OPUSD",
    "FILUSDT": "FILUSD",
    "TRXUSDT": "TRXUSD",
    "SHIBUSDT": "SHIBUSD",
    "DOGEUSDT": "XDGUSD",
    "PEPEUSDT": "PEPEUSD",
}

# Timeframe mapping: Bitget -> Kraken (in minutes for REST OHLC)
TF_TO_KRAKEN_INTERVAL: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


class SymbolMapper:
    """
    Cross-broker symbol translation utility.
    
    Handles conversion between Bitget and Kraken symbol formats,
    including support for perpetual and inverse futures on Kraken.
    """
    
    def __init__(
        self,
        product_type: KrakenProductType = KrakenProductType.PERPETUAL,
        custom_mappings: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize symbol mapper.
        
        Args:
            product_type: Kraken Futures product type (perpetual/inverse)
            custom_mappings: Additional symbol mappings to add
        """
        self.product_type = product_type
        self._bitget_to_kraken = BITGET_TO_KRAKEN_MAP.copy()
        self._kraken_to_bitget = KRAKEN_TO_BITGET_MAP.copy()
        self._bitget_to_kraken_spot = BITGET_TO_KRAKEN_SPOT_MAP.copy()
        
        if custom_mappings:
            self._bitget_to_kraken.update(custom_mappings)
            self._kraken_to_bitget = {v: k for k, v in self._bitget_to_kraken.items()}
        
        logger.debug("[SYMBOL_MAPPER] Initialized with %d mappings", len(self._bitget_to_kraken))
    
    def to_kraken(
        self,
        symbol: str,
        product_type: Optional[KrakenProductType] = None,
    ) -> str:
        """
        Convert Bitget symbol to Kraken Futures symbol.
        
        Args:
            symbol: Bitget symbol (e.g., "BTCUSDT")
            product_type: Override product type (perpetual/inverse)
            
        Returns:
            Kraken Futures symbol (e.g., "PF_XBTUSD")
            
        Example:
            mapper.to_kraken("BTCUSDT")  # "PF_XBTUSD"
            mapper.to_kraken("ETHUSDT")  # "PF_ETHUSD"
        """
        # Normalize input
        symbol = self._normalize_bitget(symbol)
        
        # Already in Kraken format — pass through to prevent double prefix
        upper = symbol.upper()
        if upper.startswith(("PF_", "PI_", "FF_", "FI_")):
            logger.debug("[SYMBOL_MAPPER] %s already Kraken format (passthrough)", symbol)
            return upper
        
        # Direct mapping lookup
        if symbol in self._bitget_to_kraken:
            kraken_symbol = self._bitget_to_kraken[symbol]
            logger.debug("[SYMBOL_MAPPER] %s -> %s (direct)", symbol, kraken_symbol)
            return kraken_symbol
        
        # Try to construct dynamically
        kraken_symbol = self._construct_kraken_symbol(symbol, product_type)
        logger.debug("[SYMBOL_MAPPER] %s -> %s (constructed)", symbol, kraken_symbol)
        return kraken_symbol
    
    def to_kraken_spot(self, symbol: str) -> str:
        """
        Convert Bitget symbol to Kraken spot/REST symbol.
        
        Used for OHLC data retrieval via REST API.
        
        Args:
            symbol: Bitget symbol (e.g., "BTCUSDT")
            
        Returns:
            Kraken spot symbol (e.g., "XXBTZUSD")
        """
        symbol = self._normalize_bitget(symbol)
        
        if symbol in self._bitget_to_kraken_spot:
            return self._bitget_to_kraken_spot[symbol]
        
        # Try to construct: BTCUSDT -> BTCUSD
        base, quote = self._split_bitget_symbol(symbol)
        
        # Kraken uses XBT for BTC
        if base == "BTC":
            base = "XBT"
        
        # Remove T from USDT
        if quote == "USDT":
            quote = "USD"
        
        return f"{base}{quote}"
    
    def to_bitget(self, symbol: str) -> str:
        """
        Convert Kraken symbol to Bitget symbol.
        
        Args:
            symbol: Kraken symbol (e.g., "PF_XBTUSD", "BTC/USD", "XXBTZUSD")
            
        Returns:
            Bitget symbol (e.g., "BTCUSDT")
            
        Example:
            mapper.to_bitget("PF_XBTUSD")  # "BTCUSDT"
            mapper.to_bitget("BTC/USD")    # "BTCUSDT"
        """
        # Normalize input
        symbol = symbol.upper().strip()
        
        # Direct mapping lookup (futures format)
        if symbol in self._kraken_to_bitget:
            bitget_symbol = self._kraken_to_bitget[symbol]
            logger.debug("[SYMBOL_MAPPER] %s -> %s (direct)", symbol, bitget_symbol)
            return bitget_symbol
        
        # Try to parse from various Kraken formats
        bitget_symbol = self._construct_bitget_symbol(symbol)
        logger.debug("[SYMBOL_MAPPER] %s -> %s (constructed)", symbol, bitget_symbol)
        return bitget_symbol
    
    def normalize(
        self,
        symbol: str,
        target_broker: BrokerType,
        source_broker: Optional[BrokerType] = None,
    ) -> str:
        """
        Normalize symbol for target broker.
        
        Auto-detects source broker format if not specified.
        
        Args:
            symbol: Input symbol
            target_broker: Target broker format
            source_broker: Source broker (auto-detected if None)
            
        Returns:
            Symbol in target broker format
        """
        if source_broker is None:
            source_broker = self._detect_broker(symbol)
        
        if source_broker == target_broker:
            return symbol
        
        if target_broker == BrokerType.KRAKEN:
            return self.to_kraken(symbol)
        else:
            return self.to_bitget(symbol)
    
    def get_canonical(self, symbol: str) -> str:
        """
        Get canonical (Bitget) symbol format for internal tracking.
        
        Args:
            symbol: Any symbol format
            
        Returns:
            Bitget-format symbol
        """
        broker = self._detect_broker(symbol)
        if broker == BrokerType.BITGET:
            return self._normalize_bitget(symbol)
        else:
            return self.to_bitget(symbol)
    
    def timeframe_to_kraken(self, tf: str) -> int:
        """
        Convert timeframe to Kraken interval (minutes).
        
        Args:
            tf: Timeframe string (e.g., "15m", "4h")
            
        Returns:
            Interval in minutes
        """
        tf = tf.lower()
        return TF_TO_KRAKEN_INTERVAL.get(tf, 15)
    
    def _detect_broker(self, symbol: str) -> BrokerType:
        """Detect broker from symbol format."""
        symbol = symbol.upper()
        
        # Kraken Futures format: PF_, PI_, FF_, FI_
        if re.match(r'^(PF|PI|FF|FI)_', symbol):
            return BrokerType.KRAKEN
        
        # Kraken spot format with slash: BTC/USD
        if '/' in symbol:
            return BrokerType.KRAKEN
        
        # Kraken spot format with X prefix: XXBTZUSD
        if symbol.startswith('X') and len(symbol) >= 6:
            return BrokerType.KRAKEN
        
        # Default to Bitget
        return BrokerType.BITGET
    
    def _normalize_bitget(self, symbol: str) -> str:
        """Normalize Bitget symbol format."""
        # Remove common suffixes/variations
        symbol = symbol.upper().strip()
        symbol = symbol.replace("_UMCBL", "")  # Remove Bitget futures suffix
        symbol = symbol.replace("-PERPETUAL", "")
        return symbol
    
    def _split_bitget_symbol(self, symbol: str) -> Tuple[str, str]:
        """Split Bitget symbol into base and quote."""
        # Common quote assets
        for quote in ["USDT", "USDC", "USD", "BUSD"]:
            if symbol.endswith(quote):
                base = symbol[:-len(quote)]
                return base, quote
        
        # Default: last 4 chars are quote
        return symbol[:-4], symbol[-4:]
    
    def _construct_kraken_symbol(
        self,
        bitget_symbol: str,
        product_type: Optional[KrakenProductType] = None,
    ) -> str:
        """Construct Kraken Futures symbol from Bitget format."""
        ptype = product_type or self.product_type
        prefix = "PF_" if ptype == KrakenProductType.PERPETUAL else "PI_"
        
        base, quote = self._split_bitget_symbol(bitget_symbol)
        
        # Kraken uses XBT for BTC
        if base == "BTC":
            base = "XBT"
        
        # Remove T from USDT for quote
        if quote == "USDT":
            quote = "USD"
        
        return f"{prefix}{base}{quote}"
    
    def _construct_bitget_symbol(self, kraken_symbol: str) -> str:
        """Construct Bitget symbol from Kraken format."""
        symbol = kraken_symbol.upper()
        
        # Remove Kraken Futures prefix
        for prefix in ["PF_", "PI_", "FF_", "FI_"]:
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
                break
        
        # Handle slash format: BTC/USD -> BTCUSD
        symbol = symbol.replace("/", "")
        
        # Remove Kraken ISO prefixes: XXBTZUSD -> XBTUSD
        if symbol.startswith("XX"):
            symbol = symbol[1:]
        if symbol.startswith("X") and len(symbol) > 6:
            symbol = symbol[1:]
        
        # Replace XBT with BTC
        symbol = symbol.replace("XBT", "BTC")
        
        # Replace XDG with DOGE
        symbol = symbol.replace("XDG", "DOGE")
        
        # Add USDT suffix if USD
        if symbol.endswith("USD") and not symbol.endswith("USDT"):
            symbol = symbol[:-3] + "USDT"
        
        # Remove Z from middle (XETHZUSD pattern)
        symbol = re.sub(r'([A-Z]+)Z([A-Z]+)', r'\1\2', symbol)
        
        return symbol
    
    def is_kraken_symbol(self, symbol: str) -> bool:
        """Check if symbol is in Kraken format."""
        return self._detect_broker(symbol) == BrokerType.KRAKEN
    
    def is_bitget_symbol(self, symbol: str) -> bool:
        """Check if symbol is in Bitget format."""
        return self._detect_broker(symbol) == BrokerType.BITGET
    
    def get_base_asset(self, symbol: str) -> str:
        """Extract base asset from any symbol format."""
        canonical = self.get_canonical(symbol)
        base, _ = self._split_bitget_symbol(canonical)
        return base
    
    def get_quote_asset(self, symbol: str) -> str:
        """Extract quote asset from any symbol format."""
        canonical = self.get_canonical(symbol)
        _, quote = self._split_bitget_symbol(canonical)
        return quote


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON & CONVENIENCE
# ══════════════════════════════════════════════════════════════════════════════

_default_mapper: Optional[SymbolMapper] = None


def get_symbol_mapper() -> SymbolMapper:
    """
    Get default symbol mapper instance.
    
    Loads custom mappings from KRAKEN_SYMBOL_MAP env var on first call.
    """
    global _default_mapper
    if _default_mapper is None:
        # Load custom mappings from env var
        env_mappings = load_kraken_symbol_map_from_env()
        _default_mapper = SymbolMapper(custom_mappings=env_mappings if env_mappings else None)
    return _default_mapper


def bitget_to_kraken(symbol: str) -> str:
    """Convenience: Convert Bitget symbol to Kraken Futures."""
    return get_symbol_mapper().to_kraken(symbol)


def kraken_to_bitget(symbol: str) -> str:
    """Convenience: Convert Kraken symbol to Bitget."""
    return get_symbol_mapper().to_bitget(symbol)


def normalize_symbol(symbol: str, broker: BrokerType) -> str:
    """Convenience: Normalize symbol for broker."""
    return get_symbol_mapper().normalize(symbol, broker)


if __name__ == "__main__":
    # Test symbol mappings
    mapper = SymbolMapper()
    
    print("="*60)
    print("Symbol Mapper Test")
    print("="*60)
    
    test_cases = [
        ("BTCUSDT", "to_kraken"),
        ("ETHUSDT", "to_kraken"),
        ("SOLUSDT", "to_kraken"),
        ("PF_XBTUSD", "to_bitget"),
        ("PF_ETHUSD", "to_bitget"),
        ("BTC/USD", "to_bitget"),
        ("XXBTZUSD", "to_bitget"),
    ]
    
    for symbol, method in test_cases:
        if method == "to_kraken":
            result = mapper.to_kraken(symbol)
        else:
            result = mapper.to_bitget(symbol)
        print(f"  {symbol:15} -> {result:15} ({method})")
    
    print("\nTimeframe conversions:")
    for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
        interval = mapper.timeframe_to_kraken(tf)
        print(f"  {tf:5} -> {interval:5} minutes")
