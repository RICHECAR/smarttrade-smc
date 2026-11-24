"""Quick verification of v11 migration."""
import sqlite3

conn = sqlite3.connect("data/trades.db")

# Check tables
cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cursor.fetchall()]
print("Has execution_attempts:", "execution_attempts" in tables)
print("Has ops_events:", "ops_events" in tables)

# Check columns
cols = [r[1] for r in conn.execute("PRAGMA table_info(execution_attempts)").fetchall()]
print("execution_attempts columns:", cols)

cols2 = [r[1] for r in conn.execute("PRAGMA table_info(ops_events)").fetchall()]
print("ops_events columns:", cols2)

# Check daily_analytics new columns
cols3 = [r[1] for r in conn.execute("PRAGMA table_info(daily_analytics)").fetchall()]
new_cols = [c for c in cols3 if "blocked" in c or "dispatch" in c or "success" in c]
print("daily_analytics new columns:", new_cols)

# Schema version
ver = conn.execute("PRAGMA user_version").fetchone()[0]
print("Schema version:", ver)

conn.close()
