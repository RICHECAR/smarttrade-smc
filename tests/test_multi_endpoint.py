import requests
import json

payload = {
    "symbol": "BTCUSDT",
    "side": "buy",
    "quantity": 5,
    "brokers": ["bitget", "kraken"],
    "dry_run": True
}

try:
    response = requests.post("http://127.0.0.1:8000/api/multi/order/test", json=payload)
    print(f"Status Code: {response.status_code}")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
