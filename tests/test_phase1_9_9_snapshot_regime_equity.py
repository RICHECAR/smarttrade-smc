"""
Phase 1.9.9 — Unit Tests for Snapshot Regime & Equity Propagation in orchestrator.py.

100% offline, zero Kraken network calls, zero .env/SQLite mutations.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from smc_futures.orchestrator import _resolve_snapshot_regime_and_equity


def test_1_cached_regime_and_confidence_extracted_instead_of_unknown():
    orch = SimpleNamespace(_cached_regime="TREND", regime_confidence=0.82)
    ctx_obj = SimpleNamespace(orchestrators={"SOLUSDT": orch}, _kraken_balance={"total": 46.6633, "available": 46.6613})

    regime, conf, eq = _resolve_snapshot_regime_and_equity(ctx_obj, loop_equity=0.0)
    assert regime == "TREND"
    assert conf == pytest.approx(0.82)
    assert eq == pytest.approx(46.6633)


def test_2_non_htf_refresh_loop_preserves_real_kraken_equity_when_loop_equity_is_zero():
    ctx_obj = SimpleNamespace(orchestrators={}, _kraken_balance={"total": 46.6633, "available": 46.6613})
    regime, conf, eq = _resolve_snapshot_regime_and_equity(ctx_obj, loop_equity=0.0)
    assert regime == "UNKNOWN"
    assert conf is None
    assert eq == pytest.approx(46.6633)


def test_3_never_invents_fictitious_equity_when_no_real_balance_exists():
    # Static ACCOUNT_BALANCE or zero/None/NaN/bool MUST yield equity=None
    for bad_bal in [None, {}, {"total": 0.0, "available": 0.0}, {"total": float("nan")}, {"total": True}]:
        ctx_obj = SimpleNamespace(orchestrators={}, _kraken_balance=bad_bal, account_state=None)
        _, _, eq = _resolve_snapshot_regime_and_equity(ctx_obj, loop_equity=0.0)
        assert eq is None


def test_4_loop_equity_takes_priority_when_freshly_refreshed():
    ctx_obj = SimpleNamespace(orchestrators={}, _kraken_balance={"total": 40.0})
    _, _, eq = _resolve_snapshot_regime_and_equity(ctx_obj, loop_equity=46.66)
    assert eq == pytest.approx(46.66)


def test_5_account_state_fallback_used_when_kraken_balance_absent():
    acc_state = SimpleNamespace(get_equity=lambda: 46.6613)
    ctx_obj = SimpleNamespace(orchestrators={}, _kraken_balance=None, account_state=acc_state)
    _, _, eq = _resolve_snapshot_regime_and_equity(ctx_obj, loop_equity=0.0)
    assert eq == pytest.approx(46.6613)


def test_6_mtf_state_regime_fallback_works_and_unknown_never_fabricated():
    orch_mtf = SimpleNamespace(mtf_state={"regime": "RANGE"})
    ctx1 = SimpleNamespace(orchestrators={"ETHUSDT": orch_mtf}, _kraken_balance=None)
    r1, c1, e1 = _resolve_snapshot_regime_and_equity(ctx1, loop_equity=0.0)
    assert r1 == "RANGE" and c1 is None and e1 is None

    orch_empty = SimpleNamespace()
    ctx2 = SimpleNamespace(orchestrators={"BTCUSDT": orch_empty}, _kraken_balance=None)
    r2, c2, e2 = _resolve_snapshot_regime_and_equity(ctx2, loop_equity=0.0)
    assert r2 == "UNKNOWN" and c2 is None and e2 is None
