#!/usr/bin/env python3
"""
Diagnostic: ACCOUNT_INCOHERENT blocker analysis.

Scans log files for ACCOUNT_INCOHERENT / ACCOUNT_STALE rejections
and produces a summary by broker, cause, and recommended fix.

Usage:
    python scripts/diagnose_account_incoherent.py [logdir]
"""

import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


# Patterns to match in log lines
_RE_BLOCK = re.compile(
    r"\[ORDER_REJECTED\]\s+(\S+)\s+\|\s+reason=(ACCOUNT_INCOHERENT|ACCOUNT_STALE)"
    r"\s+\|\s+(.+?)(?:\s+\|\s+active_broker=(\S+))?"
    r"(?:\s+\|\s+mode=(\S+))?",
)
_RE_INCOHERENT_LOG = re.compile(
    r"\[ACCOUNT_STATE\]\s+INCOHERENT\s+\|\s+(?:broker=(\w+)\s+\|)?\s*equity=([\d.\-]+)"
)
_RE_BALANCE_FAIL = re.compile(
    r"\[(?:BITGET|KRAKEN)_BALANCE\]\s+Refresh\s+failed:\s+(.+)"
)
_RE_NOT_INIT = re.compile(r"ACCOUNT_NOT_INITIALIZED")


def scan_logs(log_dir: Path):
    blocks = []          # (symbol, tag, detail, broker, mode, filename, lineno)
    incoherents = []     # (broker, equity, filename, lineno)
    refresh_fails = []   # (message, filename, lineno)
    not_inits = []       # (filename, lineno)

    log_files = sorted(log_dir.glob("**/*.log"))
    if not log_files:
        log_files = sorted(log_dir.glob("**/*.txt"))
    if not log_files:
        print(f"No log files found in {log_dir}")
        return blocks, incoherents, refresh_fails, not_inits

    for fp in log_files:
        try:
            lines = fp.read_text(errors="replace").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            m = _RE_BLOCK.search(line)
            if m:
                blocks.append((m.group(1), m.group(2), m.group(3),
                               m.group(4) or "unknown", m.group(5) or "unknown",
                               fp.name, i))
            m2 = _RE_INCOHERENT_LOG.search(line)
            if m2:
                incoherents.append((m2.group(1) or "unknown", float(m2.group(2)),
                                    fp.name, i))
            m3 = _RE_BALANCE_FAIL.search(line)
            if m3:
                refresh_fails.append((m3.group(1), fp.name, i))
            if _RE_NOT_INIT.search(line):
                not_inits.append((fp.name, i))

    return blocks, incoherents, refresh_fails, not_inits


def classify_cause(detail: str, broker: str) -> str:
    if "NOT_INITIALIZED" in detail:
        return "NOT_INITIALIZED"
    if "INCOHERENT_EQUITY:0.00" in detail:
        return f"ZERO_EQUITY_{broker.upper()}"
    if "INCOHERENT_EQUITY:-" in detail:
        return f"NEGATIVE_EQUITY_{broker.upper()}"
    if "STALE" in detail:
        return f"STALE_{broker.upper()}"
    return "OTHER"


def main():
    log_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("logs")
    if not log_dir.exists():
        print(f"Log directory not found: {log_dir}")
        sys.exit(1)

    blocks, incoherents, refresh_fails, not_inits = scan_logs(log_dir)

    print("=" * 70)
    print("  ACCOUNT_INCOHERENT DIAGNOSTIC REPORT")
    print("=" * 70)
    print()

    # 1. Total blocks
    total = len(blocks)
    stale_count = sum(1 for b in blocks if b[1] == "ACCOUNT_STALE")
    incoherent_count = sum(1 for b in blocks if b[1] == "ACCOUNT_INCOHERENT")
    print(f"  Total blocages       : {total}")
    print(f"    ACCOUNT_INCOHERENT : {incoherent_count}")
    print(f"    ACCOUNT_STALE      : {stale_count}")
    print()

    # 2. By broker
    by_broker = Counter(b[3] for b in blocks)
    print("  Par broker:")
    for broker, count in by_broker.most_common():
        print(f"    {broker:12s} : {count}")
    print()

    # 3. By cause
    causes = Counter(classify_cause(b[2], b[3]) for b in blocks)
    print("  Par cause:")
    for cause, count in causes.most_common():
        print(f"    {cause:30s} : {count}")
    print()

    # 4. Balance refresh failures
    if refresh_fails:
        fail_msgs = Counter(f[0][:80] for f in refresh_fails)
        print(f"  Balance refresh failures: {len(refresh_fails)}")
        for msg, count in fail_msgs.most_common(5):
            print(f"    [{count}x] {msg}")
    else:
        print("  Balance refresh failures: 0")
    print()

    # 5. Incoherent updates detail
    if incoherents:
        inc_brokers = Counter(i[0] for i in incoherents)
        print(f"  INCOHERENT state updates logged: {len(incoherents)}")
        for broker, count in inc_brokers.most_common():
            print(f"    {broker:12s} : {count}")
    print()

    # 6. Recommendation
    print("  " + "-" * 50)
    print("  RECOMMENDED ACTION:")
    if causes.get("ZERO_EQUITY_UNKNOWN", 0) > 0 or causes.get("ZERO_EQUITY_BITGET", 0) > 0:
        print("    → Bitget probably feeding equity=0 to shared AccountState")
        print("    → FIX: Set EXECUTION_BROKER_MODE=KRAKEN_ONLY in .env")
        print("    → FIX: Or set BITGET_ENABLED=false")
    if causes.get("NOT_INITIALIZED", 0) > 0:
        print("    → AccountState never received a valid update from active broker")
        print("    → FIX: Check that Kraken balance refresh runs before first trade")
    if causes.get("STALE_UNKNOWN", 0) + causes.get("STALE_BITGET", 0) > 0:
        print("    → Stale data from a broker that may be disabled")
        print("    → FIX: Set EXECUTION_BROKER_MODE=KRAKEN_ONLY")
    if not blocks:
        print("    → No ACCOUNT_INCOHERENT blocks found in logs")
        print("    → Either the issue is resolved or logs were rotated")
    print()


if __name__ == "__main__":
    main()
