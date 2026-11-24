import hashlib
import sqlite3
import os

with open("backend/exchanges/kraken_futures.py", "rb") as f:
    sha = hashlib.sha256(f.read()).hexdigest()
print("SEALED_SHA256:", sha)

with open(".env", "r", encoding="utf-8") as f:
    for line in f:
        if "KRAKEN_EXECUTION_ENABLED" in line:
            print("ENV_EXECUTION:", line.strip())

conn = sqlite3.connect("data/trades.db")
c = conn.cursor()
c.execute("SELECT id, order_id, symbol, status FROM live_trades WHERE status = 'QUARANTINED'")
rows = c.fetchall()
print("QUARANTINED_COUNT:", len(rows))
for r in rows:
    print(" -", r)
conn.close()

smc_dir = "smc_futures/engine/strategy"
print("SMC_MODULES:", sorted(os.listdir(smc_dir)))
