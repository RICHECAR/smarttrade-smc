import json

from backend.services.analytics_service import _sanitize_regime_breakdown


def test_sanitize_regime_breakdown_none_returns_empty_dict():
    assert _sanitize_regime_breakdown(None) == {}


def test_sanitize_regime_breakdown_string_json_parses():
    raw = json.dumps({"TREND": {"pnl": 10, "trades": 1, "wins": 1}})
    assert _sanitize_regime_breakdown(raw) == {"TREND": {"pnl": 10, "trades": 1, "wins": 1}}


def test_sanitize_regime_breakdown_string_invalid_returns_empty_dict():
    assert _sanitize_regime_breakdown("no data") == {}


def test_sanitize_regime_breakdown_invalid_type_returns_empty_dict():
    assert _sanitize_regime_breakdown([1, 2, 3]) == {}


def test_rate_limited_warning_throttles_logs():
    from backend.services.log_rate_limit import rate_limited_warning
    from unittest.mock import MagicMock
    import logging

    logger = logging.getLogger("test_rate_limit")
    logger.warning = MagicMock()

    import backend.services.log_rate_limit as limiter
    limiter._last_warning_ts = 0.0

    rate_limited_warning(logger, "test %s", "a")
    rate_limited_warning(logger, "test %s", "b")

    # Only the first warning should be invoked within the interval
    assert logger.warning.call_count == 1
