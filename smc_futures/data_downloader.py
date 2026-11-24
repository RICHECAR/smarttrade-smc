"""smc_futures.data_downloader

Utilities to download and merge OHLCV data using ccxt.

Usage:
    python -m smc_futures.data_downloader --exchange binance --symbol BTC/USDT --timeframe 1h --limit 500
"""
from typing import List, Optional
import ccxt
import pandas as pd
import time
import os


def fetch_ohlcv(exchange_name: str, symbol: str, timeframe: str = '1h', since: Optional[int] = None, limit: int = 500) -> pd.DataFrame:
    exchange_cls = getattr(ccxt, exchange_name)
    
    # Configure specifically for Bitget V2 Futures
    options = {}
    if exchange_name == 'bitget':
        options = {
            'defaultType': 'future',
            'adjustForTimeDifference': True
        }
        
    exchange = exchange_cls({
        'enableRateLimit': True,
        'options': options
    })

    # normalize symbol for ccxt (e.g., BTC/USDT)
    # Bitget V2 Futures via ccxt usually expects "BTC/USDT:USDT"
    if exchange_name == 'bitget' and ':' not in symbol and '/' in symbol:
        symbol = f"{symbol}:USDT"

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df


def save_csv(df: pd.DataFrame, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)


def merge_csv(paths: List[str], out_path: str):
    frames = [pd.read_csv(p, parse_dates=['timestamp']) for p in paths]
    merged = pd.concat(frames, axis=0, ignore_index=True)
    merged = merged.sort_values('timestamp').drop_duplicates(subset=['timestamp', 'open', 'high', 'low', 'close'])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    merged.to_csv(out_path, index=False)
    return merged


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Download OHLCV and save to CSV')
    parser.add_argument('--exchange', default='binance', help='ccxt exchange id (binance, bitget, etc)')
    parser.add_argument('--symbol', required=True, help='Trading pair e.g. BTC/USDT')
    parser.add_argument('--timeframe', default='1h')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--out', default=None, help='Output CSV file')

    args = parser.parse_args()

    df = fetch_ohlcv(args.exchange, args.symbol, timeframe=args.timeframe, limit=args.limit)
    out = args.out or f'smc_futures/data/{args.symbol.replace("/","")}_{args.timeframe}.csv'
    save_csv(df, out)
    print(f'Saved {len(df)} candles to {out}')
