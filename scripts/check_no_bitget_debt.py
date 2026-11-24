#!/usr/bin/env python3
"""
Anti-Regression Guard: No Bitget Debt in Protected Zones
=========================================================
Scans PROTECTED zones only for forbidden Bitget references.
Zones NOT listed are considered "multi-broker backend" where
"bitget" is a legitimate broker identifier.

Exit code 0 = clean, 1 = violations found.

Usage:
    python scripts/check_no_bitget_debt.py
"""

import os
import re
import sys
from pathlib import Path

# Fix encoding on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

# ── PROTECTED ZONES ─────────────────────────────────────────────────
# These files/patterns MUST be free of Bitget references.
# Any new file matching these patterns is automatically protected.
PROTECTED_PATTERNS = [
    # Frontend — fully migrated to Kraken
    "App.tsx",
    "index.html",
    "index.tsx",
    "constants.ts",
    "types.ts",
    "services/*.ts",
    "hooks/*.ts",
    "components/*.tsx",
    "pages/*.tsx",
    # Package metadata
    "package.json",
    "tsconfig.json",
    "backend/package.json",
    # Backend init files (prevent re-importing purged modules)
    "backend/exchanges/__init__.py",
    "backend/execution/__init__.py",
    "backend/engine/execution/__init__.py",
]

# ── Files with known exceptions in protected zones ──────────────────
# These protected files may contain "bitget" ONLY in data-contract types
# (matching backend API response shapes like {bitget: ..., kraken: ...}).
DATA_CONTRACT_FILES = {
    "pages/Dashboard.tsx",
    "components/PerformanceAnalyticsPanel.tsx",
}

# ── Patterns that indicate Bitget debt ──────────────────────────────
FORBIDDEN_PATTERNS = [
    re.compile(r'\bbitget\b', re.IGNORECASE),
    re.compile(r'X-BITGET-', re.IGNORECASE),
    re.compile(r'bitget-'),
    re.compile(r'BITGET_API_KEY', re.IGNORECASE),
]

# ── Data contract exceptions (allowed in DATA_CONTRACT_FILES) ───────
DATA_CONTRACT_EXCEPTIONS = [
    re.compile(r'bitget\??:\s'),
    re.compile(r'\.bitget\b'),
    re.compile(r'\[.bitget.\]'),
    re.compile(r'===?\s*.bitget.'),
    re.compile(r'r\.broker\s*===?\s*"bitget"'),
    re.compile(r'"bitget"\s*:'),
    re.compile(r"'bitget'\s*:"),
    re.compile(r'point\.bitget'),
    re.compile(r'brokers\.bitget'),
]


def resolve_protected_files():
    """Resolve glob patterns to actual file paths."""
    protected = set()
    for pattern in PROTECTED_PATTERNS:
        if "*" in pattern:
            for p in ROOT.glob(pattern):
                if p.is_file():
                    protected.add(str(p.relative_to(ROOT)).replace("\\", "/"))
        else:
            full = ROOT / pattern
            if full.is_file():
                protected.add(pattern)
    return protected


def is_data_contract_exception(filepath, line):
    """Check if this line is a known data-contract exception."""
    if filepath not in DATA_CONTRACT_FILES:
        return False
    return any(p.search(line) for p in DATA_CONTRACT_EXCEPTIONS)


def scan_file(filepath):
    """Scan a protected file for forbidden patterns."""
    violations = []
    full = ROOT / filepath
    try:
        content = full.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return violations

    for i, line in enumerate(content.splitlines(), 1):
        if is_data_contract_exception(filepath, line):
            continue
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                violations.append((filepath, i, line.strip()[:120], pattern.pattern))
                break
    return violations


def check_deleted_files():
    """Ensure purged files stay deleted."""
    MUST_NOT_EXIST = [
        "backend/bitget_v2_mix_client.py",
        "backend/exchanges/bitget_futures.py",
        "backend/brokers/bitget_broker.py",
        "backend/execution/bitget_executor.py",
        "backend/engine/execution/bitget_futures.py",
    ]
    violations = []
    for rel in MUST_NOT_EXIST:
        if (ROOT / rel).exists():
            violations.append((rel, 0, "DELETED FILE REAPPEARED", "structural"))
    return violations


def check_tailwind_no_bitget():
    """Ensure Tailwind config has no bitget color palette."""
    html = ROOT / "index.html"
    if not html.exists():
        return []
    content = html.read_text(encoding="utf-8", errors="ignore")
    if re.search(r'^\s*bitget:\s*\{', content, re.MULTILINE):
        return [("index.html", 0, "Tailwind defines 'bitget' color palette", "structural")]
    return []


def main():
    all_violations = []

    all_violations.extend(check_deleted_files())
    all_violations.extend(check_tailwind_no_bitget())

    protected = resolve_protected_files()
    for filepath in sorted(protected):
        violations = scan_file(filepath)
        all_violations.extend(violations)

    if all_violations:
        print(f"\n{'='*70}")
        print(f"  BITGET DEBT VIOLATIONS: {len(all_violations)} found")
        print(f"{'='*70}\n")

        for filepath, line_num, content, pattern in all_violations:
            print(f"  {filepath}:{line_num}")
            print(f"    Pattern: {pattern}")
            print(f"    Content: {content}")
            print()

        print(f"  Total: {len(all_violations)} violations")
        print(f"  Fix these to pass CI.\n")
        return 1
    else:
        print(f"\n  [OK] No Bitget debt in {len(protected)} protected files.")
        print(f"  Structural checks: PASS")
        print(f"  Root: {ROOT}\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
