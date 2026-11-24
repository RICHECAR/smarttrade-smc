"""
Token Bucket Rate Limiter — Proactive API Rate Control
═══════════════════════════════════════════════════════

Prevents hitting exchange rate limits by throttling requests client-side.

Kraken Futures rate limits (as of 2024):
  - Private endpoints: 500 requests per 10 seconds (50/s)
  - Public endpoints:  1000 requests per 10 seconds (100/s)
  - Order endpoints:   500 requests per 10 seconds (50/s)

Implementation: Token bucket algorithm
  - Tokens refill at a steady rate
  - Each request consumes one token
  - If no tokens available, caller waits until refill

Usage:
    from core.rate_limiter import get_rate_limiter

    limiter = get_rate_limiter("kraken")
    await limiter.acquire()  # blocks until token available
    response = await client.get(...)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("smarttrade.rate_limiter")


@dataclass
class RateLimiterConfig:
    """Rate limiter configuration."""
    max_tokens: float = 40.0       # Bucket capacity (conservative: 80% of 50/s)
    refill_rate: float = 40.0      # Tokens per second
    initial_tokens: Optional[float] = None  # Start with this many (default: max)


class TokenBucketRateLimiter:
    """
    Async token bucket rate limiter.

    Thread-safe via asyncio.Lock. Supports multiple named buckets.
    """

    def __init__(self, config: Optional[RateLimiterConfig] = None):
        self._config = config or RateLimiterConfig()
        self._tokens: float = (
            self._config.initial_tokens
            if self._config.initial_tokens is not None
            else self._config.max_tokens
        )
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()
        self._total_acquired: int = 0
        self._total_waited: float = 0.0

    async def acquire(self, tokens: float = 1.0, timeout: float = 30.0) -> bool:
        """
        Acquire tokens. Blocks until available or timeout.

        Args:
            tokens: Number of tokens to consume (default 1)
            timeout: Max wait time in seconds before giving up

        Returns:
            True if acquired, False if timed out
        """
        deadline = time.monotonic() + timeout

        while True:
            async with self._lock:
                self._refill()

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    self._total_acquired += 1
                    return True

                # Calculate wait time for enough tokens
                deficit = tokens - self._tokens
                wait_time = deficit / self._config.refill_rate

            # Check timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning(
                    "[RATE_LIMITER] Timeout after %.1fs waiting for %.1f tokens",
                    timeout, tokens,
                )
                return False

            # Wait for refill (capped by timeout)
            actual_wait = min(wait_time, remaining)
            self._total_waited += actual_wait
            await asyncio.sleep(actual_wait)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        self._tokens = min(
            self._config.max_tokens,
            self._tokens + elapsed * self._config.refill_rate,
        )

    @property
    def available_tokens(self) -> float:
        return self._tokens

    def get_stats(self) -> Dict:
        return {
            "available_tokens": round(self._tokens, 1),
            "max_tokens": self._config.max_tokens,
            "refill_rate": self._config.refill_rate,
            "total_acquired": self._total_acquired,
            "total_waited_sec": round(self._total_waited, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCES (one per exchange)
# ═══════════════════════════════════════════════════════════════════════════

_LIMITERS: Dict[str, TokenBucketRateLimiter] = {}

# Default configs per exchange
_DEFAULT_CONFIGS: Dict[str, RateLimiterConfig] = {
    "kraken_private": RateLimiterConfig(max_tokens=40.0, refill_rate=40.0),
    "kraken_public": RateLimiterConfig(max_tokens=80.0, refill_rate=80.0),
    "kraken_order": RateLimiterConfig(max_tokens=30.0, refill_rate=30.0),
}


def get_rate_limiter(name: str = "kraken_private") -> TokenBucketRateLimiter:
    """Get or create a named rate limiter."""
    if name not in _LIMITERS:
        config = _DEFAULT_CONFIGS.get(name, RateLimiterConfig())
        _LIMITERS[name] = TokenBucketRateLimiter(config)
        logger.info(
            "[RATE_LIMITER] Created '%s' (max=%.0f, rate=%.0f/s)",
            name, config.max_tokens, config.refill_rate,
        )
    return _LIMITERS[name]


def reset_all_limiters() -> None:
    """Reset all limiter instances (for testing)."""
    _LIMITERS.clear()
