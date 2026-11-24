"""
Tests for BitgetV2Client: ticker parsing, stale guard, spot rejection.
Run with:  python -m pytest tests/test_bitget_v2_client.py -v
"""

import asyncio
import time
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from smc_futures.engine.execution.bitget_v2_client import (
    BitgetV2Client,
    StaleDataError,
    _normalize,
    _validate_product_type,
    ALLOWED_PRODUCT_TYPES,
)


# ──────────────────────────────────────────────────
#  Fixtures: fake Bitget V2 responses
# ──────────────────────────────────────────────────

def _make_ticker_response(
    last_pr: str = "97312.5",
    mark_price: str = "97310.0",
    index_price: str = "97311.0",
    ts: str = None,
):
    """Build a fake Bitget V2 /mix/market/ticker response."""
    if ts is None:
        ts = str(int(time.time() * 1000))  # fresh by default
    return {
        "code": "00000",
        "msg": "success",
        "data": [{
            "symbol": "BTCUSDT",
            "lastPr": last_pr,
            "markPrice": mark_price,
            "indexPrice": index_price,
            "bestBid": "97312.0",
            "bestAsk": "97313.0",
            "baseVolume": "12345.67",
            "ts": ts,
        }],
    }


def _mock_httpx(fake_resp):
    """Create a context-managed httpx.AsyncClient mock."""
    mock = MagicMock()
    ctx = AsyncMock()
    ctx.get.return_value = fake_resp
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock.return_value = ctx
    return mock


def _fake_resp(response_dict):
    resp = MagicMock()
    resp.json.return_value = response_dict
    resp.raise_for_status = MagicMock()
    # Provide a real timedelta for elapsed so Prometheus observe() gets a float
    resp.elapsed = timedelta(milliseconds=5)
    return resp


# ──────────────────────────────────────────────────
#  1. test_parse_mark_price
# ──────────────────────────────────────────────────

def test_parse_mark_price():
    """Price should come from markPrice when PRICE_SOURCE = mark (default)."""
    async def _run():
        client = BitgetV2Client(price_source="mark")
        resp = _fake_resp(_make_ticker_response(last_pr="97312.5", mark_price="97310.0"))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price"] == 97310.0
    assert ticker["source_field"] == "markPrice"
    assert ticker["source"] == "markPrice"
    assert ticker["productType"] == "USDT-FUTURES"


# ──────────────────────────────────────────────────
#  2. test_parse_last_price
# ──────────────────────────────────────────────────

def test_parse_last_price():
    """Price should come from lastPr when PRICE_SOURCE = last."""
    async def _run():
        client = BitgetV2Client(price_source="last")
        resp = _fake_resp(_make_ticker_response(last_pr="97312.5", mark_price="97310.0"))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price"] == 97312.5
    assert ticker["source_field"] == "lastPr"
    assert ticker["stale"] is False


# ──────────────────────────────────────────────────
#  2b. test_parse_index_price
# ──────────────────────────────────────────────────

def test_parse_index_price():
    """Price should come from indexPrice when PRICE_SOURCE = index."""
    async def _run():
        client = BitgetV2Client(price_source="index")
        resp = _fake_resp(_make_ticker_response(last_pr="97312.5", index_price="97311.0"))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price"] == 97311.0
    assert ticker["source_field"] == "indexPrice"


# ──────────────────────────────────────────────────
#  3. test_stale_price_blocks_signal
# ──────────────────────────────────────────────────

def test_stale_price_blocks_signal():
    """Ticker data older than threshold should be flagged stale=True → blocks signals."""
    async def _run():
        client = BitgetV2Client(price_source="mark", stale_threshold_ms=5_000)
        old_ts = str(int(time.time() * 1000) - 30_000)  # 30s ago
        resp = _fake_resp(_make_ticker_response(ts=old_ts))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["stale"] is True
    assert ticker["age_ms"] > 5_000


# ──────────────────────────────────────────────────
#  3b. test_stale_guard_passes (fresh data)
# ──────────────────────────────────────────────────

def test_stale_guard_passes():
    """Fresh ticker data (age < threshold) should NOT be stale."""
    async def _run():
        client = BitgetV2Client(price_source="mark", stale_threshold_ms=10_000)
        fresh_ts = str(int(time.time() * 1000))
        resp = _fake_resp(_make_ticker_response(ts=fresh_ts))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["stale"] is False


# ──────────────────────────────────────────────────
#  4. test_strategy_and_ui_use_same_price
# ──────────────────────────────────────────────────

def test_strategy_and_ui_use_same_price():
    """
    Verify that a single BitgetV2Client instance returns the same price
    on consecutive calls — strategy + UI share one source.
    """
    async def _run():
        client = BitgetV2Client(price_source="mark")
        resp = _fake_resp(_make_ticker_response(
            last_pr="97312.5", mark_price="97310.0", index_price="97311.0",
        ))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            t1 = await client.get_ticker("BTCUSDT")
            t2 = await client.get_ticker("BTCUSDT")  # same call = same price
            return t1, t2

    t1, t2 = asyncio.run(_run())
    # Both calls must return identical price and source
    assert t1["price"] == t2["price"]
    assert t1["source_field"] == t2["source_field"]
    assert t1["price"] == t1["mark_price"]  # for mark source, price == mark_price
    assert t1["productType"] == "USDT-FUTURES"
    assert t2["productType"] == "USDT-FUTURES"


# ──────────────────────────────────────────────────
#  5. test_spot_price_rejected
# ──────────────────────────────────────────────────

def test_spot_price_rejected():
    """
    Any attempt to use productType=SPOT must raise ValueError immediately.
    Spot data is NEVER allowed in the pipeline.
    """
    with pytest.raises(ValueError, match="SPOT_REJECTED"):
        _validate_product_type("SPOT")

    with pytest.raises(ValueError, match="SPOT_REJECTED"):
        _validate_product_type("spot")

    with pytest.raises(ValueError, match="SPOT_REJECTED"):
        _validate_product_type("")

    # Futures types must pass without error
    _validate_product_type("USDT-FUTURES")
    _validate_product_type("usdt-futures")


# ──────────────────────────────────────────────────
#  6. test_normalize_symbol
# ──────────────────────────────────────────────────

def test_normalize_symbol():
    assert _normalize("BTC/USDT") == "BTCUSDT"
    assert _normalize("BTC:USDT") == "BTCUSDT"
    assert _normalize("BTC-USDT") == "BTCUSDT"
    assert _normalize("btcusdt") == "BTCUSDT"


# ──────────────────────────────────────────────────
#  7. test_default_is_mark
# ──────────────────────────────────────────────────

def test_default_is_mark():
    """Default price source should be 'mark' (safer for futures)."""
    client = BitgetV2Client()
    assert client.price_source == "mark"


# ──────────────────────────────────────────────────
#  8. test_ticker_seconds_conversion
# ──────────────────────────────────────────────────

def test_ticker_seconds_conversion():
    """Verify that TS < 1e12 is treated as seconds and converted to ms."""
    async def _run():
        client = BitgetV2Client()
        # 1700000000 sec = 1700000000000 ms
        resp = _fake_resp(_make_ticker_response(ts="1700000000"))
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price_ts"] == 1700000000000
    assert ticker["age_ms"] >= 0


# ──────────────────────────────────────────────────
#  9. test_ticker_age_clamped_to_zero
# ──────────────────────────────────────────────────

def test_ticker_age_clamped_to_zero():
    """Verify that if exchange_ts > received_ms (clock skew), age is 0."""
    async def _run():
        client = BitgetV2Client()
        # Mock time.time() to be BEFORE the exchange timestamp
        # Exchange says 20000, we receive at 10000 -> age would be -10000 -> clamped to 0
        future_ts = 2000000000000
        resp = _fake_resp(_make_ticker_response(ts=str(future_ts)))
        
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            with patch("time.time", return_value=1000000000.0): # 1e12 ms
                return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price_ts"] == 2000000000000
    assert ticker["age_ms"] == 0
    # Also verify we didn't break correct retrieval
    assert ticker["price"] == 97310.0


# ──────────────────────────────────────────────────
#  10. test_ticker_missing_ts_fallback
# ──────────────────────────────────────────────────

def test_ticker_missing_ts_fallback():
    """Verify fallback to received_ms if TS is missing."""
    async def _run():
        client = BitgetV2Client()
        # Force TS to be None/Empty in response
        resp_dict = _make_ticker_response()
        resp_dict["data"][0].pop("ts") # Remove ts field
        
        resp = _fake_resp(resp_dict)
        
        mock_time = 1600000000.0 # 1.6e12 ms
        with patch("httpx.AsyncClient", _mock_httpx(resp)):
            with patch("time.time", return_value=mock_time):
                return await client.get_ticker("BTCUSDT")

    ticker = asyncio.run(_run())
    assert ticker["price_ts"] == 1600000000000
    assert ticker["received_ts"] == 1600000000000
    assert ticker["age_ms"] == 0

