"""CLI helper: download recent candles, build a concise prompt and send to Ollama DeepSeek.

Example:
    python smc_futures/cli_analyze.py --symbol BTC/USDT --exchange binance --timeframe 1h --limit 120
"""
import argparse
import json
from smc_futures.data_downloader import fetch_ohlcv
from smc_futures.ollama_client import analyze_text


def build_prompt(symbol: str, df_json: str, strategy_summary: str = None) -> str:
    s = f"Analyze recent market data and provide a concise SMC signal for {symbol}.\n"
    if strategy_summary:
        s += f"Strategy summary:\n{strategy_summary}\n\n"
    s += "Recent candles (JSON array, oldest->newest):\n"
    s += df_json
    s += "\n\nPlease answer in JSON with keys: action (LONG/SHORT/HOLD), reason, confidence (0-1), entry, stop_loss, take_profit."
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exchange', default='binance')
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--timeframe', default='1h')
    parser.add_argument('--limit', type=int, default=120)
    parser.add_argument('--strategy-summary', default=None)
    args = parser.parse_args()

    df = fetch_ohlcv(args.exchange, args.symbol, timeframe=args.timeframe, limit=args.limit)
    # convert recent candles to compact JSON
    records = df.tail(100).to_dict(orient='records')
    df_json = json.dumps(records)

    prompt = build_prompt(args.symbol, df_json, args.strategy_summary)

    print('Sending prompt to Ollama... (this may take a few seconds)')
    resp = analyze_text(prompt, timeout=60)
    print('\n=== OLLAMA RESPONSE ===\n')
    print(resp)


if __name__ == '__main__':
    main()
