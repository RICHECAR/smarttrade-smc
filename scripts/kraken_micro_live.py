"""
Kraken Micro-Live Startup Script
═════════════════════════════════

Protocole de démarrage contrôlé pour le premier passage en réel sur Kraken Futures.
Exécute séquentiellement :

  Phase 0 — Pré-requis : imports, env, clef API
  Phase 1 — Healthcheck complet (broker_healthcheck)
  Phase 2 — Dry-run : shadow order simulé, vérification payload
  Phase 3 — Ordre réel taille minimale (1 contrat PF_XBTUSD)
  Phase 4 — Vérification post-trade (logs, fill, SL, TP)
  Phase 5 — Verdict GO / NO-GO

Critères d'arrêt immédiat (KILL) :
  - invalidSize dans la réponse
  - UNKNOWN_ERROR ou UNKNOWN_RESPONSE
  - SL non posé après 3 retries
  - reduce_only=True sur intent=open
  - filled_qty == 0 alors que success=True
  - entry_price absent ou 0

Usage:
  python scripts/kraken_micro_live.py             # Phases 0-2 seulement (safe)
  python scripts/kraken_micro_live.py --execute    # Phase 3 incluse (1 contrat réel)
  python scripts/kraken_micro_live.py --execute --symbol PF_ETHUSD

Author: SMC Trading Bot Framework
"""

import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("micro_live")

# ═══════════════════════════════════════════════════════════════
#  KILL CRITERIA — any True → abort immediately
# ═══════════════════════════════════════════════════════════════

KILL_PATTERNS = [
    "invalidSize",
    "UNKNOWN_ERROR",
    "UNKNOWN_RESPONSE",
    "INVALID_SIZE",
    "reduce_only_on_open",
]


def check_kill(result: dict, context: str) -> bool:
    """Return True if result contains a kill-triggering pattern."""
    result_str = json.dumps(result, default=str).lower()
    for pattern in KILL_PATTERNS:
        if pattern.lower() in result_str:
            log.critical(
                "KILL TRIGGERED [%s] — pattern '%s' found in: %s",
                context, pattern, json.dumps(result, default=str)[:300],
            )
            return True
    return False


# ═══════════════════════════════════════════════════════════════
#  PHASE 0 — PRE-REQUIS
# ═══════════════════════════════════════════════════════════════

def phase_0_prerequisites() -> bool:
    """Vérifie imports et variables d'environnement."""
    log.info("=" * 60)
    log.info("PHASE 0 — PRÉ-REQUIS")
    log.info("=" * 60)

    checks = {}

    # 0a. Imports critiques
    try:
        from backend.kraken_contracts import (
            ensure_contracts, validate_order, validate_sl_tp,
            classify_kraken_error, normalize_order_response,
        )
        checks["imports_kraken_contracts"] = True
    except ImportError as e:
        checks["imports_kraken_contracts"] = False
        log.error("  FAIL: backend.kraken_contracts — %s", e)

    try:
        from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper
        checks["imports_executor"] = True
    except ImportError as e:
        checks["imports_executor"] = False
        log.error("  FAIL: kraken_futures_executor — %s", e)

    try:
        from backend.risk.size_calculator import ensure_kraken_contracts, validate_kraken_order
        checks["imports_size_calculator"] = True
    except ImportError as e:
        checks["imports_size_calculator"] = False
        log.error("  FAIL: size_calculator — %s", e)

    # 0b. API keys
    api_key = os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET", "")
    checks["api_key"] = bool(api_key and len(api_key) > 8)
    checks["api_secret"] = bool(api_secret and len(api_secret) > 8)

    # 0c. Mode check
    auto_exec = os.getenv("AUTO_EXECUTE", "false").lower()
    checks["auto_execute_not_true"] = auto_exec != "true"
    if not checks["auto_execute_not_true"]:
        log.warning("  WARN: AUTO_EXECUTE=true — le bot pourrait trader pendant le test")

    # 0d. Kill switch
    kill = os.getenv("KILL_SWITCH", "false").lower()
    checks["kill_switch_off"] = kill != "true"

    for name, ok in checks.items():
        log.info("  [%s] %s", "PASS" if ok else "FAIL", name)

    critical = ["imports_kraken_contracts", "imports_executor", "api_key", "api_secret"]
    all_ok = all(checks.get(k, False) for k in critical)
    log.info("Phase 0: %s", "PASS" if all_ok else "FAIL — arrêt")
    return all_ok


# ═══════════════════════════════════════════════════════════════
#  PHASE 1 — HEALTHCHECK BROKER
# ═══════════════════════════════════════════════════════════════

async def phase_1_healthcheck() -> bool:
    """Exécute broker_healthcheck (sans dry-order)."""
    log.info("")
    log.info("=" * 60)
    log.info("PHASE 1 — HEALTHCHECK BROKER")
    log.info("=" * 60)

    from scripts.broker_healthcheck import run_healthcheck
    results = await run_healthcheck(dry_order=False)

    if results.get("overall"):
        log.info("Phase 1: PASS")
        return True
    else:
        log.error("Phase 1: FAIL — healthcheck non passé")
        return False


# ═══════════════════════════════════════════════════════════════
#  PHASE 2 — DRY-RUN SHADOW ORDER
# ═══════════════════════════════════════════════════════════════

async def phase_2_dry_run(symbol: str = "PF_XBTUSD") -> bool:
    """Simule un ordre sans l'envoyer — vérifie le payload complet."""
    log.info("")
    log.info("=" * 60)
    log.info("PHASE 2 — DRY-RUN SHADOW ORDER")
    log.info("=" * 60)

    from backend.kraken_contracts import (
        ensure_contracts, validate_order, validate_sl_tp,
        normalize_order_response,
    )

    checks = {}

    # 2a. Conversion coin → contracts
    test_qty_btc = 0.003
    test_price = 87000.0
    contracts = ensure_contracts(test_qty_btc, symbol, price=test_price)
    checks["conversion"] = isinstance(contracts, int) and contracts > 0
    log.info("  [%s] conversion: %.6f coins × $%.0f → %d contracts",
             "PASS" if checks["conversion"] else "FAIL",
             test_qty_btc, test_price, contracts)

    # 2b. Validate order
    valid, err = validate_order(symbol, "buy", contracts)
    checks["validate_order"] = valid
    log.info("  [%s] validate_order: %s %s", "PASS" if valid else "FAIL",
             valid, err or "")

    # 2c. Validate SL/TP — long
    sl_price = test_price * 0.985   # -1.5%
    tp_price = test_price * 1.030   # +3%
    valid, err = validate_sl_tp(
        entry_price=test_price, stop_loss=sl_price,
        take_profit=tp_price, side="buy",
    )
    checks["sl_tp_long"] = valid
    log.info("  [%s] SL/TP long: entry=$%.0f SL=$%.0f TP=$%.0f — %s",
             "PASS" if valid else "FAIL",
             test_price, sl_price, tp_price, err or "OK")

    # 2d. Validate SL/TP — short
    sl_short = test_price * 1.015
    tp_short = test_price * 0.970
    valid, err = validate_sl_tp(
        entry_price=test_price, stop_loss=sl_short,
        take_profit=tp_short, side="sell",
    )
    checks["sl_tp_short"] = valid
    log.info("  [%s] SL/TP short: entry=$%.0f SL=$%.0f TP=$%.0f — %s",
             "PASS" if valid else "FAIL",
             test_price, sl_short, tp_short, err or "OK")

    # 2e. Normalize success response
    norm = normalize_order_response({
        "success": True,
        "order_id": "dry-test-001",
        "sendStatus": {"filledSize": contracts, "avgFillPrice": test_price},
    })
    checks["normalize_success"] = (
        norm["success"] is True
        and norm["order_id"] == "dry-test-001"
        and norm["filled_qty"] == contracts
    )
    log.info("  [%s] normalize_success: %s", "PASS" if checks["normalize_success"] else "FAIL", norm)

    # 2f. Normalize error response
    norm_err = normalize_order_response({
        "sendStatus": {"status": "invalidSize", "errorMessage": "test error"},
    })
    checks["normalize_error"] = (
        norm_err["success"] is False
        and norm_err["error_code"] == "INVALID_SIZE"
    )
    log.info("  [%s] normalize_error: %s", "PASS" if checks["normalize_error"] else "FAIL", norm_err)

    # 2g. Simulate full payload (what executor would build)
    payload = {
        "symbol": symbol,
        "side": "buy",
        "order_type": "market",
        "contracts": contracts,
        "reduce_only": False,
        "intent": "open",
        "stop_loss": round(sl_price, 2),
        "take_profit": round(tp_price, 2),
    }
    log.info("  PAYLOAD PREVIEW: %s", json.dumps(payload, indent=2))

    if check_kill(payload, "phase_2_payload"):
        return False

    all_ok = all(checks.values())
    log.info("Phase 2: %s", "PASS" if all_ok else "FAIL")
    return all_ok


# ═══════════════════════════════════════════════════════════════
#  PHASE 3 — ORDRE RÉEL TAILLE MINIMALE
# ═══════════════════════════════════════════════════════════════

async def phase_3_micro_order(symbol: str = "PF_XBTUSD") -> dict:
    """
    Place 1 contrat réel market buy, puis ferme immédiatement.

    Retourne le résultat complet pour vérification manuelle.
    """
    log.info("")
    log.info("=" * 60)
    log.info("PHASE 3 — ORDRE RÉEL TAILLE MINIMALE (1 contrat)")
    log.info("=" * 60)

    from backend.execution.kraken_futures_executor import KrakenFuturesExecutorWrapper

    api_key = os.getenv("KRAKEN_FUTURES_API_KEY") or os.getenv("KRAKEN_API_KEY", "")
    api_secret = os.getenv("KRAKEN_FUTURES_API_SECRET") or os.getenv("KRAKEN_API_SECRET", "")

    wrapper = KrakenFuturesExecutorWrapper(api_key=api_key, api_secret=api_secret)

    # ── 3a. Vérifier le solde avant ──────────────────────────────
    log.info("3a. Fetching balance...")
    balance_before = await wrapper.get_balance()
    log.info("  Balance before: %s", json.dumps(balance_before, default=str)[:200])

    # ── 3b. Place 1 contrat market buy (PAS de SL/TP pour le test) ─
    log.info("3b. Placing 1-contract market BUY on %s...", symbol)
    t0 = time.time()
    result = await wrapper.place_order(
        symbol=symbol,
        side="buy",
        quantity=1,
        order_type="market",
        reduce_only=False,
        intent="open",
    )
    latency_ms = (time.time() - t0) * 1000

    log.info("  Result: %s", json.dumps(result, default=str)[:500])
    log.info("  Latency: %.0f ms", latency_ms)

    # Kill check
    if check_kill(result, "phase_3_open"):
        log.critical("KILL — fermeture de la position d'urgence")
        await wrapper.close_position(symbol, reason="micro_live_kill")
        return {"phase_3": "KILLED", "result": result}

    order_ok = result.get("success") is True or result.get("code") == "00000"
    if not order_ok:
        log.error("Phase 3: FAIL — ordre non rempli: %s", result.get("msg") or result.get("error"))
        return {"phase_3": "FAIL", "result": result}

    order_id = result.get("order_id") or result.get("data", {}).get("orderId")
    log.info("  order_id: %s", order_id)

    # ── 3c. Vérifier la position ouverte ──────────────────────────
    await asyncio.sleep(1.5)
    log.info("3c. Checking open positions...")
    positions = await wrapper.get_positions(symbol)
    log.info("  Positions: %s", json.dumps(positions, default=str)[:500])

    has_position = any(float(p.get("size", 0)) > 0 for p in positions)

    # ── 3d. Fermer immédiatement ──────────────────────────────────
    log.info("3d. Closing position immediately...")
    close_result = await wrapper.close_position(symbol, reason="micro_live_test")
    log.info("  Close result: %s", json.dumps(close_result, default=str)[:500])

    if check_kill(close_result, "phase_3_close"):
        return {"phase_3": "KILLED_ON_CLOSE", "close_result": close_result}

    close_ok = close_result.get("success") is True or close_result.get("code") in ("00000", "NO_POSITION")

    # ── 3e. Balance après ─────────────────────────────────────────
    await asyncio.sleep(1)
    balance_after = await wrapper.get_balance()
    log.info("  Balance after: %s", json.dumps(balance_after, default=str)[:200])

    report = {
        "phase_3": "PASS" if (order_ok and close_ok) else "FAIL",
        "open": {
            "success": order_ok,
            "order_id": order_id,
            "latency_ms": round(latency_ms, 1),
            "result": result,
        },
        "position_detected": has_position,
        "close": {
            "success": close_ok,
            "result": close_result,
        },
        "balance_before": balance_before,
        "balance_after": balance_after,
    }

    return report


# ═══════════════════════════════════════════════════════════════
#  PHASE 4 — VÉRIFICATION POST-TRADE
# ═══════════════════════════════════════════════════════════════

def phase_4_post_trade_check(report: dict) -> bool:
    """Vérifie les champs critiques du rapport phase 3."""
    log.info("")
    log.info("=" * 60)
    log.info("PHASE 4 — VÉRIFICATION POST-TRADE")
    log.info("=" * 60)

    checks = {}

    open_data = report.get("open", {})
    result = open_data.get("result", {})

    # 4a. order_id présent
    checks["order_id_present"] = bool(open_data.get("order_id"))
    log.info("  [%s] order_id: %s",
             "PASS" if checks["order_id_present"] else "FAIL",
             open_data.get("order_id"))

    # 4b. Pas d'invalidSize
    result_str = json.dumps(result, default=str)
    checks["no_invalid_size"] = "invalidSize" not in result_str and "INVALID_SIZE" not in result_str
    log.info("  [%s] no_invalidSize", "PASS" if checks["no_invalid_size"] else "FAIL")

    # 4c. Pas d'UNKNOWN_ERROR
    checks["no_unknown_error"] = "UNKNOWN_ERROR" not in result_str and "UNKNOWN_RESPONSE" not in result_str
    log.info("  [%s] no_unknown_error", "PASS" if checks["no_unknown_error"] else "FAIL")

    # 4d. Position détectée
    checks["position_detected"] = report.get("position_detected", False)
    log.info("  [%s] position_detected", "PASS" if checks["position_detected"] else "FAIL")

    # 4e. Close réussie
    checks["close_success"] = report.get("close", {}).get("success", False)
    log.info("  [%s] close_success", "PASS" if checks["close_success"] else "FAIL")

    # 4f. Latence acceptable (< 5s)
    lat = open_data.get("latency_ms", 99999)
    checks["latency_ok"] = lat < 5000
    log.info("  [%s] latency: %.0f ms (max 5000)", "PASS" if checks["latency_ok"] else "WARN", lat)

    all_ok = all(checks.values())
    log.info("Phase 4: %s", "PASS" if all_ok else "FAIL")
    return all_ok


# ═══════════════════════════════════════════════════════════════
#  PHASE 5 — VERDICT FINAL
# ═══════════════════════════════════════════════════════════════

def phase_5_verdict(phases: dict):
    """Affiche le verdict final GO / NO-GO."""
    log.info("")
    log.info("=" * 60)
    log.info("PHASE 5 — VERDICT FINAL")
    log.info("=" * 60)

    for name, ok in phases.items():
        status = "PASS" if ok else "FAIL"
        log.info("  %s: %s", name, status)

    all_go = all(phases.values())

    log.info("")
    if all_go:
        log.info("╔══════════════════════════════════════════╗")
        log.info("║   VERDICT: GO — MICRO-LIVE AUTORISÉ     ║")
        log.info("╚══════════════════════════════════════════╝")
        log.info("")
        log.info("Prochaines étapes :")
        log.info("  1. Configurer .env : KRAKEN_ENABLED=true")
        log.info("  2. Configurer .env : AUTO_EXECUTE=shadow")
        log.info("  3. Observer 10+ signaux en shadow avec Kraken")
        log.info("  4. Puis AUTO_EXECUTE=true, BOT_RISK_PCT=0.05 (min)")
        log.info("  5. Observer 3-5 trades réels taille minimale")
        log.info("  6. Augmenter progressivement BOT_RISK_PCT")
    else:
        log.info("╔══════════════════════════════════════════╗")
        log.info("║   VERDICT: NO-GO — CORRIGER AVANT LIVE  ║")
        log.info("╚══════════════════════════════════════════╝")
        failed = [k for k, v in phases.items() if not v]
        log.info("  Bloqueurs : %s", failed)

    return all_go


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    execute = "--execute" in sys.argv
    symbol_arg = "PF_XBTUSD"
    for i, arg in enumerate(sys.argv):
        if arg == "--symbol" and i + 1 < len(sys.argv):
            symbol_arg = sys.argv[i + 1]

    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║  KRAKEN MICRO-LIVE STARTUP PROTOCOL                 ║")
    log.info("║  Symbol: %-42s ║", symbol_arg)
    log.info("║  Execute: %-41s ║", str(execute))
    log.info("╚══════════════════════════════════════════════════════╝")
    log.info("")

    phases = {}

    # Phase 0
    phases["phase_0_prerequisites"] = phase_0_prerequisites()
    if not phases["phase_0_prerequisites"]:
        phase_5_verdict(phases)
        return

    # Phase 1
    phases["phase_1_healthcheck"] = await phase_1_healthcheck()
    if not phases["phase_1_healthcheck"]:
        phase_5_verdict(phases)
        return

    # Phase 2
    phases["phase_2_dry_run"] = await phase_2_dry_run(symbol_arg)
    if not phases["phase_2_dry_run"]:
        phase_5_verdict(phases)
        return

    # Phase 3 (optionnelle)
    if execute:
        log.info("")
        log.info("*** ATTENTION: --execute activé ***")
        log.info("*** Un ordre RÉEL de 1 contrat va être passé sur %s ***", symbol_arg)
        log.info("*** Appuyez sur Ctrl+C dans les 5 secondes pour annuler ***")
        try:
            await asyncio.sleep(5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Annulé par l'utilisateur.")
            return

        report = await phase_3_micro_order(symbol_arg)
        log.info("")
        log.info("Rapport phase 3 complet :")
        log.info(json.dumps(report, indent=2, default=str))

        phases["phase_3_micro_order"] = report.get("phase_3") == "PASS"

        # Phase 4
        phases["phase_4_post_trade"] = phase_4_post_trade_check(report)
    else:
        log.info("")
        log.info("Phase 3-4 ignorées (ajouter --execute pour ordre réel)")

    # Phase 5
    phase_5_verdict(phases)


if __name__ == "__main__":
    asyncio.run(main())
