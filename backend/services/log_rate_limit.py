"""Simple rate-limited logging helpers.

This is useful in production to avoid flooding logs when a condition can occur many times per
second, while still allowing a counter/metric to track the full event rate.

Usage:
    from backend.services.log_rate_limit import rate_limited_warning
    rate_limited_warning(logger, "... %s", value)
"""

import time

_last_warning_ts = 0.0
WARNING_INTERVAL_SEC = 60.0


def rate_limited_warning(logger, msg: str, *args, **kwargs):
    """Emit a warning at most once every WARNING_INTERVAL_SEC seconds."""

    global _last_warning_ts
    now = time.time()
    if now - _last_warning_ts > WARNING_INTERVAL_SEC:
        logger.warning(msg, *args, **kwargs)
        _last_warning_ts = now
