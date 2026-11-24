#!/usr/bin/env python3
"""
go_live_check.py — Single CLI for go-live validation.
═════════════════════════════════════════════════════════

Phase 11: Unified entry point for all pre-deployment checks.

Usage:
  python scripts/go_live_check.py                   # Full check
  python scripts/go_live_check.py --preflight       # Preflight only
  python scripts/go_live_check.py --drills           # Failure drills only
  python scripts/go_live_check.py --backup           # Create backup
  python scripts/go_live_check.py --dashboard        # Ops dashboard snapshot
  python scripts/go_live_check.py --capital          # Capital readiness

Exit codes:
  0 = READY (go live)
  1 = DEGRADED (proceed with caution)
  2 = BLOCKED (do NOT go live)
"""

import argparse
import json
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_preflight() -> int:
    """Run all preflight checks."""
    from core.preflight import PreflightChecker, PreflightVerdict

    print("=" * 60)
    print("  PREFLIGHT CHECKS")
    print("=" * 60)

    checker = PreflightChecker()
    report = checker.run_all()

    for check in report.checks:
        icon = "OK" if check.passed else "FAIL"
        sev = f"[{check.severity.value.upper()}]"
        print(f"  [{icon}] {sev:12s} {check.name:20s} {check.message}")

    print("-" * 60)
    print(f"  VERDICT: {report.verdict.value}")
    print(f"  Passed: {report.passed_count}/{len(report.checks)} ({report.duration_sec:.3f}s)")

    if report.critical_failures:
        print(f"\n  CRITICAL BLOCKERS ({len(report.critical_failures)}):")
        for cf in report.critical_failures:
            print(f"    - {cf.name}: {cf.message}")

    print()

    if report.verdict == PreflightVerdict.BLOCKED:
        return 2
    elif report.verdict == PreflightVerdict.DEGRADED:
        return 1
    return 0


def run_drills() -> int:
    """Run failure drills."""
    from core.failure_drills import FailureDrillRunner

    print("=" * 60)
    print("  FAILURE DRILLS")
    print("=" * 60)

    runner = FailureDrillRunner()
    report = runner.run_all()

    for result in report.results:
        icon = "OK" if result.passed else "FAIL"
        print(f"  [{icon}] {result.name:35s} {result.message} ({result.duration_sec:.3f}s)")
        if result.error:
            print(f"         ERROR: {result.error}")

    print("-" * 60)
    print(f"  Drills: {report.passed}/{report.total} passed ({report.duration_sec:.3f}s)")
    print()

    return 0 if report.failed == 0 else 2


def run_backup() -> int:
    """Create a backup."""
    from core.backup_manager import get_backup_manager

    print("=" * 60)
    print("  BACKUP")
    print("=" * 60)

    try:
        mgr = get_backup_manager()
        record = mgr.create_backup()
        print(f"  Created: {record.filename}")
        print(f"  Size:    {record.size_bytes / 1024:.1f} KB")
        print(f"  SHA256:  {record.sha256[:24]}...")
        print(f"  Integrity: {'OK' if record.integrity_ok else 'FAIL'}")
        print(f"  WAL:     {'included' if record.wal_included else 'not included'}")
        print(f"  Time:    {record.duration_sec:.3f}s")

        status = mgr.get_status()
        print(f"\n  Total backups: {status.backup_count}")
        print(f"  Free disk: {status.free_disk_mb:.0f} MB")
        print()
        return 0
    except Exception as e:
        print(f"  ERROR: {e}")
        print()
        return 2


def run_dashboard() -> int:
    """Print ops dashboard snapshot."""
    from core.ops_dashboard import get_ops_dashboard

    print("=" * 60)
    print("  OPS DASHBOARD")
    print("=" * 60)

    dashboard = get_ops_dashboard()
    snapshot = dashboard.get_snapshot()

    print(json.dumps(snapshot, indent=2, default=str))
    print()
    return 0


def run_capital() -> int:
    """Print capital readiness assessment."""
    from core.ops_dashboard import get_ops_dashboard

    print("=" * 60)
    print("  CAPITAL READINESS")
    print("=" * 60)

    dashboard = get_ops_dashboard()
    snapshot = dashboard.get_snapshot()
    cap = snapshot.get("capital_readiness", {})

    print(f"  Current Tier: {cap.get('tier', 'unknown')}")
    print(f"  Max Capital:  {cap.get('max_capital_eur', 0):.0f} EUR")
    print(f"\n  Conditions:")
    for cond, met in cap.get("conditions", {}).items():
        icon = "OK" if met else "FAIL"
        print(f"    [{icon}] {cond}")

    if cap.get("blockers"):
        print(f"\n  Blockers:")
        for b in cap["blockers"]:
            print(f"    - {b}")

    print(f"\n  Recommendation: {cap.get('recommendation', 'N/A')}")
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Go-Live Check — Phase 11 ops-grade validation",
    )
    parser.add_argument("--preflight", action="store_true", help="Run preflight checks only")
    parser.add_argument("--drills", action="store_true", help="Run failure drills only")
    parser.add_argument("--backup", action="store_true", help="Create backup")
    parser.add_argument("--dashboard", action="store_true", help="Show ops dashboard")
    parser.add_argument("--capital", action="store_true", help="Show capital readiness")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    args = parser.parse_args()

    # If no specific flag, run everything
    run_all = not any([args.preflight, args.drills, args.backup, args.dashboard, args.capital])

    exit_code = 0

    if run_all or args.backup:
        code = run_backup()
        exit_code = max(exit_code, code)

    if run_all or args.preflight:
        code = run_preflight()
        exit_code = max(exit_code, code)

    if run_all or args.drills:
        code = run_drills()
        exit_code = max(exit_code, code)

    if run_all or args.capital:
        code = run_capital()
        exit_code = max(exit_code, code)

    if args.dashboard:
        code = run_dashboard()
        exit_code = max(exit_code, code)

    # Final verdict
    if run_all:
        print("=" * 60)
        if exit_code == 0:
            print("  FINAL VERDICT: READY — GO FOR LIVE")
        elif exit_code == 1:
            print("  FINAL VERDICT: DEGRADED — PROCEED WITH CAUTION")
        else:
            print("  FINAL VERDICT: BLOCKED — DO NOT GO LIVE")
        print("=" * 60)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
