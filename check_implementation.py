#!/usr/bin/env python3
"""
REAL ONLY Implementation Helper
This script helps verify and track which files need modification.

Usage:
    python check_implementation.py

Status check:
    ✅ File modified correctly
    ⚠️  File needs review
    ❌ File missing or broken
"""

import os
import re
from pathlib import Path


class ImplementationChecker:
    def __init__(self, workspace_root="."):
        self.root = Path(workspace_root)
        self.checks = []

    def check_file_exists(self, path: str, name: str) -> bool:
        """Check if file exists."""
        full_path = self.root / path
        exists = full_path.exists()
        status = "✅" if exists else "❌"
        self.checks.append((status, f"File exists: {name}"))
        return exists

    def check_file_contains(self, path: str, pattern: str, name: str) -> bool:
        """Check if file contains pattern."""
        full_path = self.root / path
        if not full_path.exists():
            self.checks.append(("❌", f"File missing: {name}"))
            return False

        with open(full_path, "r") as f:
            content = f.read()
            found = pattern in content or re.search(pattern, content, re.DOTALL)
            status = "✅" if found else "⚠️ "
            self.checks.append((status, f"Pattern found: {name}"))
            return found

    def run_all_checks(self):
        """Run all implementation checks."""
        print("\n" + "="*70)
        print("REAL ONLY IMPLEMENTATION STATUS CHECK")
        print("="*70 + "\n")

        # 1. Config
        print("📋 CONFIGURATION CHECKS")
        print("-" * 70)
        self.check_file_exists("smc_futures/smc_config.py", "smc_config.py exists")
        self.check_file_contains(
            "smc_futures/smc_config.py",
            r"REAL_ONLY_MODE\s*=",
            "REAL_ONLY_MODE defined"
        )
        self.check_file_contains(
            "smc_futures/smc_config.py",
            r"AUTO_EXECUTE_ENABLED\s*and\s*_TRADING_MODE\s*==\s*['\"]paper['\"]",
            "Paper mode contradiction check"
        )
        self.check_file_contains(
            "smc_futures/smc_config.py",
            r"MAX_TRADES_PER_DAY.*=.*1",
            "Conservative max trades default"
        )

        # 2. Order Manager
        print("\n📋 ORDER MANAGER CHECKS")
        print("-" * 70)
        self.check_file_exists("smc_futures/engine/execution/order_manager.py", "order_manager.py exists")
        self.check_file_contains(
            "smc_futures/engine/execution/order_manager.py",
            r"async def execute\(",
            "execute() method exists"
        )
        self.check_file_contains(
            "smc_futures/engine/execution/order_manager.py",
            r"[AUTO_EXEC].*PRE-FLIGHT",
            "Auto-exec logging present"
        )

        # 3. Orchestrator
        print("\n📋 ORCHESTRATOR CHECKS")
        print("-" * 70)
        self.check_file_exists("smc_futures/orchestrator.py", "orchestrator.py exists")
        self.check_file_contains(
            "smc_futures/orchestrator.py",
            r"order_mgr\.execute\(",
            "order_manager.execute() called"
        )
        self.check_file_contains(
            "smc_futures/orchestrator.py",
            r"\[AUTO_EXEC\].*HOOK TRIGGERED",
            "Auto-exec hook logging"
        )

        # 4. Bitget Executor
        print("\n📋 BITGET EXECUTOR CHECKS")
        print("-" * 70)
        self.check_file_exists("smc_futures/engine/execution/bitget_executor.py", "bitget_executor.py exists")
        self.check_file_contains(
            "smc_futures/engine/execution/bitget_executor.py",
            r"\[HTTP_CALL\]",
            "HTTP_CALL logging"
        )
        self.check_file_contains(
            "smc_futures/engine/execution/bitget_executor.py",
            r"orderType.*market.*\(no price\)",
            "Market order comment"
        )
        self.check_file_contains(
            "smc_futures/engine/execution/bitget_executor.py",
            r"timeInForce.*GTC",
            "Limit order GTC"
        )

        # 5. API Engine
        print("\n📋 API ENGINE CHECKS")
        print("-" * 70)
        self.check_file_exists("smc_futures/api_smc_engine.py", "api_smc_engine.py exists")
        self.check_file_contains(
            "smc_futures/api_smc_engine.py",
            r"@app\.get\(['\"]\/health",
            "/health endpoint"
        )
        self.check_file_contains(
            "smc_futures/api_smc_engine.py",
            r"@app\.get\(['\"]\/api\/status",
            "/api/status endpoint"
        )
        self.check_file_contains(
            "smc_futures/api_smc_engine.py",
            r"@app\.post\(['\"]\/api\/bot\/kill-switch",
            "/api/bot/kill-switch endpoint"
        )

        # 6. Tests
        print("\n📋 TEST FILES CHECKS")
        print("-" * 70)
        self.check_file_exists("tests/test_real_only_fail_fast.py", "test_real_only_fail_fast.py exists")
        self.check_file_exists("tests/test_bitget_v2_payloads.py", "test_bitget_v2_payloads.py exists")
        self.check_file_exists("tests/test_auto_exec_hook.py", "test_auto_exec_hook.py exists")

        # 7. Documentation
        print("\n📋 DOCUMENTATION CHECKS")
        print("-" * 70)
        self.check_file_exists("GO_LIVE_CHECKLIST_REAL_ONLY.md", "GO_LIVE_CHECKLIST_REAL_ONLY.md exists")
        self.check_file_exists("IMPLEMENTATION_GUIDE_REAL_ONLY.md", "IMPLEMENTATION_GUIDE_REAL_ONLY.md exists")

        # Print results
        print("\n" + "="*70)
        print("DETAILED RESULTS")
        print("="*70 + "\n")

        for status, message in self.checks:
            print(f"{status} {message}")

        # Summary
        passed = sum(1 for s, _ in self.checks if s == "✅")
        total = len(self.checks)
        pending = sum(1 for s, _ in self.checks if s == "⚠️ ")

        print("\n" + "="*70)
        print(f"SUMMARY: {passed}/{total} checks passed")
        if pending > 0:
            print(f"⚠️  {pending} checks need review")
        print("="*70 + "\n")

        return passed == total


def main():
    """Run the checker."""
    checker = ImplementationChecker()
    success = checker.run_all_checks()

    if success:
        print("🎉 ALL CHECKS PASSED! Ready for testing.")
        print("\nNext steps:")
        print("  1. pytest tests/test_real_only_fail_fast.py -v")
        print("  2. pytest tests/test_bitget_v2_payloads.py -v")
        print("  3. pytest tests/test_auto_exec_hook.py -v")
        print("  4. python -m uvicorn smc_futures.api_smc_engine:app --port 8000")
        return 0
    else:
        print("⚠️  SOME CHECKS FAILED. See above for details.")
        print("\nRefer to IMPLEMENTATION_GUIDE_REAL_ONLY.md for instructions.")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
