"""Quick check of Bitget funding rate API data."""
import httpx, statistics

r = httpx.get('https://api.bitget.com/api/v2/mix/market/history-fund-rate', params={
    'symbol': 'BTCUSDT', 'productType': 'USDT-FUTURES', 'pageSize': '100'
})
data = r.json().get('data', [])
print(f"BTC Funding entries: {len(data)}")
if data:
    rates = [float(x['fundingRate']) for x in data]
    print(f"  Latest: ts={data[0]['fundingTime']} rate={data[0]['fundingRate']}")
    print(f"  Oldest: ts={data[-1]['fundingTime']} rate={data[-1]['fundingRate']}")
    print(f"  Mean:   {statistics.mean(rates)*100:.4f}%")
    print(f"  Std:    {statistics.stdev(rates)*100:.4f}%")
    print(f"  Min:    {min(rates)*100:.4f}%")
    print(f"  Max:    {max(rates)*100:.4f}%")
    extreme_pos = sum(1 for r in rates if r > 0.0003)
    extreme_neg = sum(1 for r in rates if r < -0.0001)
    print(f"  Extreme+ (>0.03%): {extreme_pos}/{len(rates)}")
    print(f"  Extreme- (<-0.01%): {extreme_neg}/{len(rates)}")

for sym in ['ETHUSDT', 'SOLUSDT']:
    r2 = httpx.get('https://api.bitget.com/api/v2/mix/market/history-fund-rate', params={
        'symbol': sym, 'productType': 'USDT-FUTURES', 'pageSize': '100'
    })
    d2 = r2.json().get('data', [])
    if d2:
        rates2 = [float(x['fundingRate']) for x in d2]
        print(f"\n{sym}: {len(d2)} entries, mean={statistics.mean(rates2)*100:.4f}%, std={statistics.stdev(rates2)*100:.4f}%")

# Also check basis (perp vs spot)
print("\n--- BASIS CHECK ---")
import time
# Get last 4h candle for perp and spot
for source, url, params in [
    ("Perp", "https://api.bitget.com/api/v2/mix/market/candles",
     {"symbol": "BTCUSDT", "productType": "usdt-futures", "granularity": "4H", "limit": "3"}),
    ("Spot", "https://api.bitget.com/api/v2/spot/market/candles",
     {"symbol": "BTCUSDT", "granularity": "4h", "limit": "3"}),
]:
    r3 = httpx.get(url, params=params)
    d3 = r3.json().get('data', [])
    if d3:
        print(f"  {source}: ts={d3[0][0]} close={d3[0][4]}")
