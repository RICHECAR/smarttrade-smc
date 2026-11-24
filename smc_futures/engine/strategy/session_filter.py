"""
Session Filter — Config-driven London + New York session gate.
Rejects any trade attempt outside allowed sessions with structured logging.
"""

import logging
from datetime import datetime, timezone
from typing import List

from smc_futures.engine.utils.reason_codes import ReasonCode

logger = logging.getLogger(__name__)


class SessionFilter:
    """
    Filters trading activity based on major market sessions.
    SMC strategies rely on institutional volume, which peaks during:
        - London  (08:00 – 16:00 UTC)
        - New York (13:00 – 21:00 UTC)
    Overlap (13:00 – 16:00 UTC) is highest-volume.

    Trades outside these hours are rejected with reason SESSION_OUTSIDE.
    """

    # session name → (start_hour_utc, end_hour_utc)
    SESSION_WINDOWS = {
        "london": (8, 16),
        "ny":     (13, 21),
    }

    def __init__(self, allowed_sessions: List[str] | None = None):
        if allowed_sessions is None:
            allowed_sessions = ["london", "ny"]
        self.allowed_sessions = [s.lower() for s in allowed_sessions]

    # ──────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────

    def is_in_session(self, timestamp_ms: int) -> bool:
        """Check if *timestamp_ms* falls inside any allowed session."""
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        # FM-7: Weekend filter — institutional volume near-zero
        if dt.weekday() >= 5:  # 5=Saturday, 6=Sunday
            return False
        hour = dt.hour
        for name in self.allowed_sessions:
            window = self.SESSION_WINDOWS.get(name)
            if window and window[0] <= hour < window[1]:
                return True
        return False

    def get_session_name(self, timestamp_ms: int) -> str:
        """Return the name of the active session(s), or 'ASIAN/OFF'."""
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        hour = dt.hour

        in_london = self.SESSION_WINDOWS["london"][0] <= hour < self.SESSION_WINDOWS["london"][1]
        in_ny     = self.SESSION_WINDOWS["ny"][0]     <= hour < self.SESSION_WINDOWS["ny"][1]

        if in_london and in_ny:
            return "LONDON_NY_OVERLAP"
        if in_london:
            return "LONDON"
        if in_ny:
            return "NEW_YORK"
        return "ASIAN/OFF"

    def check_and_log(self, symbol: str, timestamp_ms: int) -> bool:
        """
        Gate check with structured log on rejection.
        Returns True if allowed, False + log if rejected.
        """
        if self.is_in_session(timestamp_ms):
            return True

        session_name = self.get_session_name(timestamp_ms)
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        logger.info(
            f"[FILTER] {symbol} | REJECTED"
            f" | reason={ReasonCode.SESSION_OUTSIDE}"
            f" | session={session_name}"
            f" | hour={dt.hour}"
        )
        return False
