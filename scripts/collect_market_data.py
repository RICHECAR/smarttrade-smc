"""
Collecte intégrale des données de marché → MySQL
══════════════════════════════════════════════════

Récupère TOUT l'historique disponible (OHLCV) depuis les API exchanges
et le stocke dans MySQL pour backtesting.

Fonctionnalités :
  ✓ Collecte multi-symboles, multi-timeframes en parallèle
  ✓ Pagination automatique (200 bougies/requête × N pages)
  ✓ Reprise automatique (ne re-télécharge pas ce qui existe déjà)
  ✓ Mode continu (--live) pour maintenir les données à jour
  ✓ Suivi de progression dans la table collection_status
  ✓ Rate limiting intelligent (respect des limites API)
  ✓ Support Bitget + Kraken

Usage :
    # Collecte historique complète (90 jours par défaut)
    python scripts/collect_market_data.py

    # Collecte 180 jours d'historique
    python scripts/collect_market_data.py --days 180

    # Mode temps réel (tourne en boucle, mise à jour chaque minute)
    python scripts/collect_market_data.py --live

    # Spécifier des symboles
    python scripts/collect_market_data.py --symbols BTCUSDT,ETHUSDT

    # Spécifier des timeframes
    python scripts/collect_market_data.py --timeframes 1m,5m,15m,1h,4h

    # Collecte via Kraken au lieu de Bitget
    python scripts/collect_market_data.py --broker kraken
"""

import asyncio
import argparse
import logging
import os
import sys
import time
import signal as signal_module
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ajouter le répertoire racine au path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from backend.db.mysql_models import (
    Candle, FundingRate, TickerSnapshot, CollectionStatus,
    get_mysql_engine, get_mysql_session, MarketBase,
)

logger = logging.getLogger("market_collector")

# ══════════════════════════════════════════════════════════════
#  CONSTANTES
# ══════════════════════════════════════════════════════════════

# Timeframe → durée en millisecondes
TF_DURATION_MS = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}

# Bitget TF → granularity API
BITGET_TF_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1Dutc", "1w": "1Wutc",
}

# Kraken TF → interval en minutes
KRAKEN_TF_MAP = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440, "1w": 10080,
}

# Symboles Bitget → format API
BITGET_ALL_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT", "MATICUSDT",
    "BNBUSDT", "LTCUSDT", "ARBUSDT", "OPUSDT", "APTUSDT",
    "SUIUSDT", "NEARUSDT", "FILUSDT", "ATOMUSDT", "INJUSDT",
    "TIAUSDT", "SEIUSDT", "WIFUSDT", "PENDLEUSDT", "JUPUSDT",
    "ENAUSDT", "STXUSDT", "PYTHUSDT", "WUSDT", "ONDOUSDT",
]

# Kraken symbols mapping (Bitget → Kraken spot pour OHLC public)
KRAKEN_SYMBOL_MAP = {
    "BTCUSDT": "XXBTZUSD",
    "ETHUSDT": "XETHZUSD",
    "SOLUSDT": "SOLUSD",
    "XRPUSDT": "XXRPZUSD",
    "DOGEUSDT": "XDGUSD",
    "ADAUSDT": "ADAUSD",
    "AVAXUSDT": "AVAXUSD",
    "DOTUSDT": "DOTUSD",
    "LINKUSDT": "LINKUSD",
    "MATICUSDT": "MATICUSD",
    "LTCUSDT": "XLTCZUSD",
}

BATCH_SIZE = 200          # Bitget max par requête
RATE_LIMIT_DELAY = 0.35   # Délai entre requêtes API (sec)
BULK_INSERT_SIZE = 500    # Nombre de lignes par INSERT batch

# ══════════════════════════════════════════════════════════════
#  COLLECTEURS par broker
# ══════════════════════════════════════════════════════════════

import httpx

BITGET_BASE = "https://api.bitget.com"
KRAKEN_BASE = "https://api.kraken.com"

# Signal d'arrêt propre
_shutdown = False
def _handle_sigint(sig, frame):
    global _shutdown
    _shutdown = True
    print("\n⏹ Arrêt demandé. Finalisation en cours...")
signal_module.signal(signal_module.SIGINT, _handle_sigint)


async def fetch_bitget_candles(
    symbol: str, tf: str, end_time: Optional[int] = None
) -> List[Dict]:
    """
    Récupère un batch de 200 bougies depuis Bitget.
    Retourne : [{ts, open, high, low, close, volume}, ...]
    """
    gran = BITGET_TF_MAP.get(tf, tf)
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": symbol,
        "productType": "usdt-futures",
        "granularity": gran,
        "limit": "200",
    }
    if end_time:
        params["endTime"] = str(end_time)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()

    body = resp.json()
    if body.get("code") != "00000":
        raise RuntimeError(f"Bitget API error: {body.get('msg', body)}")

    rows = body.get("data", [])
    candles = []
    for r in rows:
        candles.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5]) if len(r) > 5 else 0.0,
        })
    candles.sort(key=lambda c: c["ts"])
    return candles


async def fetch_kraken_candles(
    symbol: str, tf: str, since: Optional[int] = None
) -> List[Dict]:
    """
    Récupère des bougies depuis l'API publique Kraken.
    """
    kraken_sym = KRAKEN_SYMBOL_MAP.get(symbol, symbol)
    interval = KRAKEN_TF_MAP.get(tf)
    if interval is None:
        logger.warning(f"Kraken ne supporte pas le TF '{tf}', skip")
        return []

    url = f"{KRAKEN_BASE}/0/public/OHLC"
    params = {"pair": kraken_sym, "interval": interval}
    if since:
        params["since"] = since

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()

    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken API error: {body['error']}")

    result = body.get("result", {})
    pair_key = [k for k in result.keys() if k != "last"]
    if not pair_key:
        return []

    raw = result[pair_key[0]]
    candles = []
    for c in raw:
        candles.append({
            "ts": int(c[0]) * 1000,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[6]),
        })
    candles.sort(key=lambda c: c["ts"])
    return candles


# ══════════════════════════════════════════════════════════════
#  INSERT en masse dans MySQL (UPSERT)
# ══════════════════════════════════════════════════════════════

def bulk_upsert_candles(session, candles: List[Dict], broker: str,
                        symbol: str, timeframe: str) -> int:
    """
    Insère des bougies en masse avec ON DUPLICATE KEY UPDATE.
    Retourne le nombre de lignes insérées/mises à jour.
    """
    if not candles:
        return 0

    inserted = 0
    tf_ms = TF_DURATION_MS.get(timeframe, 60_000)

    for i in range(0, len(candles), BULK_INSERT_SIZE):
        batch = candles[i:i + BULK_INSERT_SIZE]
        values = []
        for c in batch:
            values.append({
                "broker": broker,
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": c["ts"],
                "close_time": c["ts"] + tf_ms,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            })

        stmt = mysql_insert(Candle).values(values)
        stmt = stmt.on_duplicate_key_update(
            open=stmt.inserted.open,
            high=stmt.inserted.high,
            low=stmt.inserted.low,
            close=stmt.inserted.close,
            volume=stmt.inserted.volume,
            close_time=stmt.inserted.close_time,
        )
        session.execute(stmt)
        inserted += len(batch)

    session.commit()
    return inserted


# ══════════════════════════════════════════════════════════════
#  COLLECTE HISTORIQUE — un symbole / un timeframe
# ══════════════════════════════════════════════════════════════

async def collect_symbol_tf(
    broker: str, symbol: str, timeframe: str,
    days: int, session, progress_callback=None,
) -> int:
    """
    Collecte toutes les bougies disponibles pour un symbole/TF.
    Utilise la pagination arrière (endTime) pour remonter dans le temps.
    """
    global _shutdown

    tf_ms = TF_DURATION_MS.get(timeframe, 60_000)
    now_ms = int(time.time() * 1000)
    target_start = now_ms - (days * 86_400_000)

    # Vérifier ce qu'on a déjà
    existing = session.query(CollectionStatus).filter_by(
        broker=broker, symbol=symbol, timeframe=timeframe
    ).first()

    if existing and existing.oldest_ts and existing.oldest_ts <= target_start:
        # On a déjà tout → juste mettre à jour les dernières bougies
        end_time = None  # fetch les plus récentes
        if existing.newest_ts:
            logger.info(f"  [{symbol}/{timeframe}] Mise à jour depuis {existing.newest_ts}")
    else:
        end_time = None  # commencer par les plus récentes

    total_inserted = 0
    page = 0
    earliest_ts = now_ms
    latest_ts = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3

    while not _shutdown:
        try:
            if broker == "kraken":
                # Kraken utilise 'since' (timestamp en secondes)
                since_ts = None
                if end_time:
                    since_ts = (end_time // 1000) - (BATCH_SIZE * (tf_ms // 1000))
                candles = await fetch_kraken_candles(symbol, timeframe, since_ts)
            else:
                candles = await fetch_bitget_candles(symbol, timeframe, end_time)
            consecutive_errors = 0  # Reset on success
        except Exception as e:
            consecutive_errors += 1
            logger.error(f"  [{symbol}/{timeframe}] Erreur API page {page} "
                         f"(tentative {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.error(f"  [{symbol}/{timeframe}] Abandon après {MAX_CONSECUTIVE_ERRORS} erreurs consécutives")
                break
            await asyncio.sleep(2)
            continue

        if not candles:
            break

        # Insérer dans MySQL
        count = bulk_upsert_candles(session, candles, broker, symbol, timeframe)
        total_inserted += count
        page += 1

        # Tracker les bornes
        batch_min = min(c["ts"] for c in candles)
        batch_max = max(c["ts"] for c in candles)
        earliest_ts = min(earliest_ts, batch_min)
        latest_ts = max(latest_ts, batch_max)

        if progress_callback:
            progress_callback(symbol, timeframe, page, total_inserted)

        # Vérifier si on a atteint la cible
        if earliest_ts <= target_start:
            break

        # Prochaine page : aller plus loin dans le passé
        if broker == "kraken":
            # Kraken retourne tout d'un coup, pas de pagination réelle
            break
        else:
            end_time = batch_min - 1
            if len(candles) < BATCH_SIZE:
                break  # Plus de données disponibles

        # Rate limiting
        await asyncio.sleep(RATE_LIMIT_DELAY)

    # Mettre à jour le statut
    if not existing:
        existing = CollectionStatus(
            broker=broker, symbol=symbol, timeframe=timeframe,
        )
        session.add(existing)

    if total_inserted > 0:
        existing.oldest_ts = earliest_ts
        existing.newest_ts = latest_ts
        existing.total_candles = session.query(Candle).filter_by(
            broker=broker, symbol=symbol, timeframe=timeframe
        ).count()
        existing.last_fetch_at = datetime.utcnow()
        existing.status = "done"
    session.commit()

    return total_inserted


# ══════════════════════════════════════════════════════════════
#  COLLECTE COMPLÈTE — tous symboles × tous timeframes
# ══════════════════════════════════════════════════════════════

async def collect_all(
    broker: str,
    symbols: List[str],
    timeframes: List[str],
    days: int,
):
    """Lance la collecte pour tous les symboles × timeframes."""
    global _shutdown

    engine = get_mysql_engine()
    MarketBase.metadata.create_all(bind=engine)
    session = get_mysql_session(engine)

    total_tasks = len(symbols) * len(timeframes)
    completed = 0
    total_candles = 0

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   COLLECTE INTÉGRALE DES DONNÉES DE MARCHÉ             ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Broker     : {broker}")
    print(f"  Symboles   : {len(symbols)} ({', '.join(symbols[:5])}{'...' if len(symbols) > 5 else ''})")
    print(f"  Timeframes : {', '.join(timeframes)}")
    print(f"  Historique : {days} jours")
    print(f"  Total      : {total_tasks} tâches")
    print(f"  Ctrl+C pour arrêter proprement")
    print()

    start_time = time.time()

    for symbol in symbols:
        if _shutdown:
            break

        for tf in timeframes:
            if _shutdown:
                break

            completed += 1
            pct = completed / total_tasks * 100

            def progress(sym, t, page, count):
                elapsed = time.time() - start_time
                print(f"\r  [{completed}/{total_tasks}] {pct:5.1f}% | "
                      f"{sym}/{t} page {page} ({count:,} bougies) | "
                      f"{elapsed:.0f}s", end="", flush=True)

            try:
                n = await collect_symbol_tf(
                    broker, symbol, tf, days, session,
                    progress_callback=progress,
                )
                total_candles += n

                if n > 0:
                    print(f"\r  [{completed}/{total_tasks}] {pct:5.1f}% | "
                          f"{symbol}/{tf} ✅ {n:,} bougies                  ")
                else:
                    print(f"\r  [{completed}/{total_tasks}] {pct:5.1f}% | "
                          f"{symbol}/{tf} ⏭️  (déjà à jour)              ")
            except Exception as e:
                print(f"\r  [{completed}/{total_tasks}] {pct:5.1f}% | "
                      f"{symbol}/{tf} ❌ {e}                          ")

                # Enregistrer l'erreur
                cs = session.query(CollectionStatus).filter_by(
                    broker=broker, symbol=symbol, timeframe=tf
                ).first()
                if not cs:
                    cs = CollectionStatus(
                        broker=broker, symbol=symbol, timeframe=tf
                    )
                    session.add(cs)
                cs.status = "error"
                cs.error_msg = str(e)[:500]
                session.commit()

    elapsed = time.time() - start_time
    session.close()

    print()
    print("═" * 58)
    print(f"  COLLECTE TERMINÉE")
    print(f"  Bougies collectées : {total_candles:,}")
    print(f"  Durée totale       : {elapsed:.1f}s")
    print(f"  Tâches complétées  : {completed}/{total_tasks}")
    if _shutdown:
        print(f"  ⚠️  Arrêt anticipé par l'utilisateur")
    print("═" * 58)


# ══════════════════════════════════════════════════════════════
#  MODE LIVE — Mise à jour continue
# ══════════════════════════════════════════════════════════════

async def live_update_loop(
    broker: str,
    symbols: List[str],
    timeframes: List[str],
    interval_sec: int = 60,
):
    """Boucle de mise à jour continue des données."""
    global _shutdown

    engine = get_mysql_engine()
    session = get_mysql_session(engine)

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   MODE TEMPS RÉEL — Collecte continue                  ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"  Intervalle : {interval_sec}s")
    print(f"  Ctrl+C pour arrêter")
    print()

    cycle = 0
    while not _shutdown:
        cycle += 1
        cycle_start = time.time()
        updated = 0

        for symbol in symbols:
            if _shutdown:
                break
            for tf in timeframes:
                if _shutdown:
                    break

                try:
                    if broker == "kraken":
                        candles = await fetch_kraken_candles(symbol, tf)
                    else:
                        candles = await fetch_bitget_candles(symbol, tf)

                    if candles:
                        # Prendre seulement les 10 dernières pour mise à jour
                        recent = candles[-10:]
                        n = bulk_upsert_candles(session, recent, broker, symbol, tf)
                        updated += n

                    await asyncio.sleep(RATE_LIMIT_DELAY)
                except Exception as e:
                    logger.warning(f"[LIVE] {symbol}/{tf} erreur: {e}")

        elapsed = time.time() - cycle_start
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] Cycle {cycle} — {updated} bougies mises à jour ({elapsed:.1f}s)")

        # Attendre l'intervalle
        wait = max(0, interval_sec - elapsed)
        for _ in range(int(wait)):
            if _shutdown:
                break
            await asyncio.sleep(1)

    session.close()
    print("\n  ⏹ Mode temps réel arrêté")


# ══════════════════════════════════════════════════════════════
#  STATISTIQUES
# ══════════════════════════════════════════════════════════════

def show_stats():
    """Affiche les statistiques de la base de données."""
    engine = get_mysql_engine()
    session = get_mysql_session(engine)

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   STATISTIQUES DE LA BASE DE DONNÉES MARCHÉ            ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    # Total de bougies
    total = session.query(Candle).count()
    print(f"  Total bougies : {total:,}")

    # Par symbole
    result = session.execute(text(
        "SELECT symbol, timeframe, COUNT(*) as cnt, "
        "MIN(open_time) as oldest, MAX(open_time) as newest "
        "FROM candles GROUP BY symbol, timeframe ORDER BY symbol, timeframe"
    ))
    rows = result.fetchall()

    current_sym = None
    for row in rows:
        sym, tf, cnt, oldest, newest = row
        if sym != current_sym:
            current_sym = sym
            print(f"\n  📊 {sym}")
        oldest_dt = datetime.utcfromtimestamp(oldest / 1000).strftime("%Y-%m-%d")
        newest_dt = datetime.utcfromtimestamp(newest / 1000).strftime("%Y-%m-%d")
        print(f"     {tf:>5s} : {cnt:>8,} bougies | {oldest_dt} → {newest_dt}")

    # Statuts de collecte
    statuses = session.query(CollectionStatus).all()
    errors = [s for s in statuses if s.status == "error"]
    if errors:
        print(f"\n  ⚠️  {len(errors)} tâches en erreur :")
        for e in errors:
            print(f"     → {e.symbol}/{e.timeframe} : {e.error_msg[:80]}")

    session.close()
    print()


# ══════════════════════════════════════════════════════════════
#  EXPORT CSV
# ══════════════════════════════════════════════════════════════

def export_csv(symbol: str, timeframe: str, output_dir: str = "data/exports"):
    """Exporte les données d'un symbole/TF en CSV pour analyse externe."""
    import csv

    engine = get_mysql_engine()
    session = get_mysql_session(engine)

    os.makedirs(output_dir, exist_ok=True)

    candles = session.query(Candle).filter_by(
        symbol=symbol, timeframe=timeframe
    ).order_by(Candle.open_time).all()

    if not candles:
        print(f"  Aucune donnée pour {symbol}/{timeframe}")
        return

    filename = f"{output_dir}/{symbol}_{timeframe}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "datetime", "open", "high", "low", "close", "volume"])
        for c in candles:
            dt = datetime.utcfromtimestamp(c.open_time / 1000).isoformat()
            writer.writerow([c.open_time, dt, c.open, c.high, c.low, c.close, c.volume])

    print(f"  ✅ Exporté {len(candles):,} bougies → {filename}")
    session.close()


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Collecte intégrale des données de marché → MySQL",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--days", type=int,
        default=int(os.getenv("COLLECT_HISTORY_DAYS", "90")),
        help="Nombre de jours d'historique à récupérer (défaut: 90)"
    )
    parser.add_argument(
        "--symbols", type=str,
        default=os.getenv("COLLECT_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT"),
        help="Symboles à collecter (séparés par virgule)"
    )
    parser.add_argument(
        "--timeframes", type=str,
        default=os.getenv("COLLECT_TIMEFRAMES", "1m,5m,15m,30m,1h,4h,1d"),
        help="Timeframes à collecter"
    )
    parser.add_argument(
        "--broker", type=str,
        default=os.getenv("COLLECT_BROKER", "bitget"),
        choices=["bitget", "kraken"],
        help="Broker source (bitget ou kraken)"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Mode temps réel (mise à jour continue)"
    )
    parser.add_argument(
        "--interval", type=int,
        default=int(os.getenv("COLLECT_LIVE_INTERVAL_SEC", "60")),
        help="Intervalle de mise à jour en mode live (secondes)"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Afficher les statistiques de la base de données"
    )
    parser.add_argument(
        "--export", type=str, nargs=2, metavar=("SYMBOL", "TF"),
        help="Exporter un symbole/TF en CSV (ex: --export BTCUSDT 1h)"
    )
    parser.add_argument(
        "--all-symbols", action="store_true",
        help="Collecter TOUS les symboles disponibles (30+)"
    )
    return parser.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    args = parse_args()

    if args.stats:
        show_stats()
        return

    if args.export:
        export_csv(args.export[0], args.export[1])
        return

    symbols = args.symbols.split(",") if isinstance(args.symbols, str) else args.symbols
    timeframes = args.timeframes.split(",") if isinstance(args.timeframes, str) else args.timeframes

    if args.all_symbols:
        symbols = BITGET_ALL_SYMBOLS

    # Filtrer les TF non supportés selon le broker
    if args.broker == "kraken":
        supported = set(KRAKEN_TF_MAP.keys())
        filtered = [tf for tf in timeframes if tf in supported]
        if len(filtered) < len(timeframes):
            removed = set(timeframes) - set(filtered)
            print(f"  ⚠️  TF non supportés par Kraken retirés : {removed}")
        timeframes = filtered
        # Filtrer les symboles non mappés
        symbols = [s for s in symbols if s in KRAKEN_SYMBOL_MAP]

    if args.live:
        asyncio.run(live_update_loop(
            args.broker, symbols, timeframes, args.interval
        ))
    else:
        asyncio.run(collect_all(args.broker, symbols, timeframes, args.days))


if __name__ == "__main__":
    main()
