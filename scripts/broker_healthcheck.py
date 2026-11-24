"""
Broker Healthcheck — Validates Kraken Futures connectivity before trading.
═══════════════════════════════════════════════════════════════════════════

Run: python -m scripts.broker_healthcheck [--dry-order]

Checks:
  1. API key present and non-empty
  2. REST client instantiation
  3. Authentication (fetch balance)
  4. Fetch open positions
  5. Symbol resolution (mapping works)
  6. Contract model validation (ensure_contracts, validate_order)
  7. (Optional) Dry test order — minimal market buy 1 contract

Author: SMC Trading Bot Framework
"""

import asyncio
import logging
import os
import sys

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("healthcheck")


async def run_healthcheck(dry_order: bool = False) -> dict:
    """Run full broker healthcheck. Returns {check: pass/fail} dict."""
    results = {}

    # ── 1. API keys present ───────────────────────────────────────
    api_key = os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET", "")
    results["api_key_present"] = bool(api_key and len(api_key) > 8)
    results["api_secret_present"] = bool(api_secret and len(api_secret) > 8)
    log.info("[1] API key present:    %s", "PASS" if results["api_key_present"] else "FAIL")
    log.info("[1] API secret present: %s", "PASS" if results["api_secret_present"] else "FAIL")

    if not results["api_key_present"]:
        log.error("    -> Set KRAKEN_FUTURES_API_KEY in .env")
        results["overall"] = False
        return results

    # ── 2. Client instantiation ───────────────────────────────────
    client = None
    client_type = "unknown"
    try:
        from backend.exchanges.kraken_futures import KrakenFuturesClient
        client = KrakenFuturesClient(api_key=api_key, api_secret=api_secret)
        client_type = "KrakenFuturesClient"
        results["client_init"] = True
    except Exception as e1:
        try:
            from backend.kraken_futures_rest_v2 import KrakenFuturesRESTClientV2
            client = KrakenFuturesRESTClientV2(api_key=api_key, api_secret=api_secret)
            client_type = "KrakenFuturesRESTClientV2"
            results["client_init"] = True
        except Exception as e2:
            results["client_init"] = False
            log.error("[2] Client init: FAIL — %s / %s", e1, e2)

    log.info("[2] Client init: %s (%s)", "PASS" if results.get("client_init") else "FAIL", client_type)

    # ── 3. Authentication (balance fetch) ─────────────────────────
    try:
        balance = await client.get_balance()
        results["auth_balance"] = balance is not None
        if balance:
            log.info("[3] Auth + balance: PASS — %s", str(balance)[:120])
        else:
            log.warning("[3] Auth + balance: WARN — returned None")
    except Exception as e:
        results["auth_balance"] = False
        log.error("[3] Auth + balance: FAIL — %s", e)

    # ── 4. Positions fetch ────────────────────────────────────────
    try:
        positions = await client.get_positions()
        results["positions"] = True
        log.info("[4] Positions: PASS — %d open", len(positions) if positions else 0)
    except Exception as e:
        results["positions"] = False
        log.error("[4] Positions: FAIL — %s", e)

    # ── 5. Symbol resolution ──────────────────────────────────────
    from backend.kraken_contracts import to_kraken_symbol, from_kraken_symbol
    test_mappings = {
        "BTCUSDT": "PF_XBTUSD",
        "ETHUSDT": "PF_ETHUSD",
        "SOLUSDT": "PF_SOLUSD",
        "PF_XBTUSD": "PF_XBTUSD",
    }
    all_sym_ok = True
    for src, expected in test_mappings.items():
        resolved = to_kraken_symbol(src)
        ok = resolved == expected
        all_sym_ok = all_sym_ok and ok
        log.info("[5] Symbol: %s -> %s  %s", src, resolved, "PASS" if ok else "FAIL")

    # Roundtrip check
    roundtrip = from_kraken_symbol(to_kraken_symbol("BTCUSDT")) == "BTCUSDT"
    results["symbol_resolution"] = all_sym_ok and roundtrip
    log.info("[5] Roundtrip: %s", "PASS" if roundtrip else "FAIL")

    # ── 6. Contract model validation ──────────────────────────────
    from backend.kraken_contracts import (
        ensure_contracts, validate_order, validate_sl_tp,
        classify_kraken_error, normalize_order_response, coin_to_contracts,
    )
    contract_ok = True

    # 6a. ensure_contracts: fractional BTC → integer contracts
    c1 = ensure_contracts(0.003, "PF_XBTUSD", price=87000.0)
    check_6a = isinstance(c1, int) and c1 == 261
    contract_ok = contract_ok and check_6a
    log.info("[6a] ensure_contracts(0.003, PF_XBTUSD, 87k) = %d  %s", c1, "PASS" if check_6a else "FAIL")

    # 6b. ensure_contracts: already integer → passthrough
    c2 = ensure_contracts(250, "PF_ETHUSD")
    check_6b = c2 == 250
    contract_ok = contract_ok and check_6b
    log.info("[6b] ensure_contracts(250, PF_ETHUSD) = %d  %s", c2, "PASS" if check_6b else "FAIL")

    # 6c. validate_order: valid order passes
    valid, err = validate_order("PF_XBTUSD", "buy", 100)
    check_6c = valid and err is None
    contract_ok = contract_ok and check_6c
    log.info("[6c] validate_order(PF_XBTUSD, buy, 100) = %s  %s", valid, "PASS" if check_6c else "FAIL")

    # 6d. validate_order: 0 contracts blocked
    valid, err = validate_order("PF_XBTUSD", "buy", 0)
    check_6d = not valid and "INVALID_SIZE" in (err or "")
    contract_ok = contract_ok and check_6d
    log.info("[6d] validate_order(PF_XBTUSD, buy, 0) = %s  %s", valid, "PASS" if check_6d else "FAIL")

    # 6e. validate_sl_tp: long SL coherence
    valid, err = validate_sl_tp(entry_price=100.0, stop_loss=95.0, take_profit=110.0, side="buy")
    check_6e = valid
    contract_ok = contract_ok and check_6e
    log.info("[6e] validate_sl_tp(long, SL=95, TP=110, entry=100) = %s  %s", valid, "PASS" if check_6e else "FAIL")

    # 6f. validate_sl_tp: invalid long SL blocked
    valid, err = validate_sl_tp(entry_price=100.0, stop_loss=105.0, take_profit=None, side="buy")
    check_6f = not valid
    contract_ok = contract_ok and check_6f
    log.info("[6f] validate_sl_tp(long, SL=105, entry=100) = %s  %s", valid, "PASS" if check_6f else "FAIL")

    # 6g. error classification
    ec = classify_kraken_error("invalidSize", "")
    check_6g = ec == "INVALID_SIZE"
    contract_ok = contract_ok and check_6g
    log.info("[6g] classify_kraken_error('invalidSize') = %s  %s", ec, "PASS" if check_6g else "FAIL")

    # 6h. response normalization
    norm = normalize_order_response({"success": True, "order_id": "abc123"})
    check_6h = norm["success"] is True and norm["order_id"] == "abc123"
    contract_ok = contract_ok and check_6h
    log.info("[6h] normalize_order_response(success) = %s  %s", norm["success"], "PASS" if check_6h else "FAIL")

    results["contract_model"] = contract_ok

    # ── 7. Dry test order (optional) ──────────────────────────────
    if dry_order:
        log.info("[7] DRY ORDER TEST — placing 1-contract market buy on PF_XBTUSD...")
        try:
            from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
            wrapper = KrakenFuturesExecutorWrapper(api_key=api_key, api_secret=api_secret)
            result = await wrapper.place_order(
                symbol="BTCUSDT",
                side="buy",
                quantity=1,
                order_type="market",
                reduce_only=False,
                intent="open",
            )
            order_ok = result.get("success") is True or result.get("code") == "00000"
            results["dry_order"] = order_ok
            log.info("[7] Dry order: %s — %s", "PASS" if order_ok else "FAIL", result)

            if order_ok:
                # Immediately close
                await asyncio.sleep(1)
                close_result = await wrapper.close_position("BTCUSDT", reason="healthcheck_cleanup")
                log.info("[7] Cleanup close: %s", close_result.get("success", close_result.get("code")))
        except Exception as e:
            results["dry_order"] = False
            log.error("[7] Dry order: FAIL — %s", e)
    else:
        log.info("[7] Dry order: SKIP (use --dry-order to enable)")

    # ── Overall verdict ───────────────────────────────────────────
    critical = ["api_key_present", "client_init", "auth_balance", "symbol_resolution", "contract_model"]
    results["overall"] = all(results.get(k, False) for k in critical)

    log.info("=" * 60)
    if results["overall"]:
        log.info("VERDICT: ALL CHECKS PASSED — PRODUCTION READY")
    else:
        failed = [k for k in critical if not results.get(k, False)]
        log.error("VERDICT: BLOCKED — failed: %s", failed)
    log.info("=" * 60)

    return results


if __name__ == "__main__":
    dry = "--dry-order" in sys.argv
    asyncio.run(run_healthcheck(dry_order=dry))
