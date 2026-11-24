"""
Anti-Regression Test: Bitget Debt Guard
========================================
Ensures no new Bitget references leak into protected zones.
These tests MUST pass in CI — any failure means someone reintroduced debt.
"""

import subprocess
import sys
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestNoBitgetDebt:
    """Verify Bitget has been eradicated from protected zones."""

    def test_no_bitget_css_classes(self):
        """No bitget- CSS class prefix in any frontend file."""
        frontend_files = list(ROOT.glob("*.tsx")) + \
                         list(ROOT.glob("components/*.tsx")) + \
                         list(ROOT.glob("pages/*.tsx")) + \
                         list(ROOT.glob("*.html"))
        violations = []
        for f in frontend_files:
            content = f.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(content.splitlines(), 1):
                if "bitget-" in line and "kraken-" not in line:
                    violations.append(f"{f.name}:{i}: {line.strip()[:80]}")
        assert not violations, f"Bitget CSS classes found:\n" + "\n".join(violations)

    def test_no_bitget_headers_in_frontend(self):
        """No X-BITGET-* headers in TypeScript service files."""
        for f in ROOT.glob("services/*.ts"):
            content = f.read_text(encoding="utf-8", errors="ignore")
            assert "X-BITGET-" not in content, \
                f"{f.name} still contains X-BITGET-* headers"

    def test_no_bitget_headers_in_orchestrator(self):
        """No X-BITGET-* header aliases in Python API endpoints."""
        orch = ROOT / "smc_futures" / "orchestrator_with_ollama.py"
        if orch.exists():
            content = orch.read_text(encoding="utf-8", errors="ignore")
            assert 'alias="X-BITGET-' not in content, \
                "orchestrator_with_ollama.py still has X-BITGET-* header aliases"

    def test_tailwind_no_bitget_theme(self):
        """Tailwind config should not define bitget color palette."""
        html = ROOT / "index.html"
        content = html.read_text(encoding="utf-8", errors="ignore")
        # Check there's no "bitget:" color definition
        assert re.search(r'^\s*bitget:\s*\{', content, re.MULTILINE) is None, \
            "index.html still defines a 'bitget' Tailwind color palette"

    def test_deleted_executors_stay_deleted(self):
        """Purged executor files must not reappear."""
        deleted_files = [
            "backend/bitget_v2_mix_client.py",
            "backend/exchanges/bitget_futures.py",
            "backend/brokers/bitget_broker.py",
            "backend/execution/bitget_executor.py",
            "backend/engine/execution/bitget_futures.py",
        ]
        for rel_path in deleted_files:
            full = ROOT / rel_path
            assert not full.exists(), \
                f"Deleted file reappeared: {rel_path}"

    def test_no_bitget_imports_in_init_files(self):
        """__init__.py files must not import from deleted Bitget modules."""
        init_files = [
            ROOT / "backend" / "exchanges" / "__init__.py",
            ROOT / "backend" / "execution" / "__init__.py",
        ]
        forbidden_imports = [
            "bitget_executor",
            "bitget_futures",
            "BitgetExecutorWrapper",
            "BitgetFuturesClient",
            "create_bitget_executor",
        ]
        for init_file in init_files:
            if not init_file.exists():
                continue
            content = init_file.read_text(encoding="utf-8", errors="ignore")
            for forbidden in forbidden_imports:
                assert forbidden not in content, \
                    f"{init_file.name} still imports '{forbidden}'"

    def test_server_cjs_uses_neutral_headers(self):
        """server.cjs should extract X-API-* headers, not X-BITGET-* exclusively."""
        server = ROOT / "backend" / "server.cjs"
        content = server.read_text(encoding="utf-8", errors="ignore")
        assert "X-API-KEY" in content, \
            "server.cjs does not use X-API-KEY header"
        assert "X-API-SECRET" in content, \
            "server.cjs does not use X-API-SECRET header"

    def test_debt_scanner_passes(self):
        """The debt scanner script must exit cleanly."""
        script = ROOT / "scripts" / "check_no_bitget_debt.py"
        if not script.exists():
            pytest.skip("Debt scanner script not found")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, \
            f"Debt scanner found violations:\n{result.stdout}\n{result.stderr}"

    def test_enum_no_bitget_broker(self):
        """Frontend BrokerName enum should not have BITGET entry."""
        hook_file = ROOT / "hooks" / "useAnalytics.ts"
        if hook_file.exists():
            content = hook_file.read_text(encoding="utf-8", errors="ignore")
            assert 'BITGET = "bitget"' not in content, \
                "BrokerName enum still has BITGET entry"

    def test_kraken_service_no_bitget_refs(self):
        """krakenService.ts should be free of Bitget references."""
        svc = ROOT / "services" / "krakenService.ts"
        content = svc.read_text(encoding="utf-8", errors="ignore")
        # Check for any bitget reference (case insensitive)
        matches = re.findall(r'bitget', content, re.IGNORECASE)
        assert not matches, \
            f"krakenService.ts still has {len(matches)} Bitget reference(s)"
