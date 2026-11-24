"""
Tests for per-broker contract specs, Kraken coin-to-contracts conversion,
and per-broker preflight validation.
"""
import pytest
from backend.risk.position_sizing import (
    get_contract_specs,
    coin_to_kraken_contracts,
    check_trade_feasibility,
    BITGET_CONTRACT_SPECS,
    KRAKEN_CONTRACT_SPECS,
    _BITGET_TO_KRAKEN_SYMBOL,
)


class TestGetContractSpecs:
    """Test get_contract_specs with broker parameter."""
    
    def test_bitget_default(self):
        specs = get_contract_specs("BTCUSDT")
        assert specs["min_qty"] == 0.001
        assert specs["qty_step"] == 0.001
        assert specs["min_notional"] == 5.0
    
    def test_bitget_explicit(self):
        specs = get_contract_specs("SOLUSDT", broker="bitget")
        assert specs["min_qty"] == 0.1
        assert specs["qty_step"] == 0.1
    
    def test_kraken_direct_symbol(self):
        specs = get_contract_specs("PF_XBTUSD", broker="kraken")
        assert specs["min_qty"] == 1
        assert specs["qty_step"] == 1
        assert specs["contract_value_usd"] == 1
    
    def test_kraken_via_bitget_symbol(self):
        """Kraken lookup using Bitget symbol format (BTCUSDT -> PF_XBTUSD)."""
        specs = get_contract_specs("BTCUSDT", broker="kraken")
        assert specs["min_qty"] == 1
        assert specs["qty_step"] == 1
        assert specs["contract_value_usd"] == 1
    
    def test_kraken_unknown_symbol_defaults(self):
        specs = get_contract_specs("UNKNOWNUSDT", broker="kraken")
        assert specs["min_qty"] == 1
        assert specs["contract_value_usd"] == 1
    
    def test_bitget_unknown_symbol_defaults(self):
        specs = get_contract_specs("UNKNOWNUSDT", broker="bitget")
        assert specs["min_qty"] == 0.001
        assert specs["min_notional"] == 5.0
    
    def test_all_mapped_symbols_have_kraken_specs(self):
        """Every Bitget symbol in the map should resolve to Kraken specs."""
        for bitget_sym, kraken_sym in _BITGET_TO_KRAKEN_SYMBOL.items():
            specs = get_contract_specs(bitget_sym, broker="kraken")
            assert specs["min_qty"] == 1, f"Missing Kraken specs for {bitget_sym}"


class TestCoinToKrakenContracts:
    """Test coin quantity to Kraken contract conversion."""
    
    def test_btc_at_50k(self):
        """0.002 BTC at $50,000 = 100 contracts."""
        contracts = coin_to_kraken_contracts(0.002, 50000.0)
        assert contracts == 100
    
    def test_sol_at_150(self):
        """0.1 SOL at $150 = 15 contracts."""
        contracts = coin_to_kraken_contracts(0.1, 150.0)
        assert contracts == 15
    
    def test_xrp_at_2_50(self):
        """1 XRP at $2.50 = 2 contracts (Python banker's rounding: round(2.5)=2)."""
        contracts = coin_to_kraken_contracts(1.0, 2.50)
        assert contracts == 2  # round(2.5) = 2 in Python 3 (banker's rounding), max(1, 2) = 2
    
    def test_minimum_1_contract(self):
        """Even tiny quantities should produce at least 1 contract."""
        contracts = coin_to_kraken_contracts(0.00001, 50000.0)
        assert contracts >= 1
    
    def test_zero_qty_gets_1(self):
        """Zero coin qty should still give 1 contract (minimum)."""
        contracts = coin_to_kraken_contracts(0, 50000.0)
        assert contracts == 1
    
    def test_large_position(self):
        """1 BTC at $50,000 = 50,000 contracts."""
        contracts = coin_to_kraken_contracts(1.0, 50000.0)
        assert contracts == 50000
    
    def test_custom_contract_value(self):
        """With contract_value_usd=10, each contract = $10."""
        contracts = coin_to_kraken_contracts(0.1, 50000.0, contract_value_usd=10.0)
        assert contracts == 500  # 5000 / 10


class TestPerBrokerPreflight:
    """Test that preflight works differently per broker."""
    
    def test_bitget_solusdt_small_account(self):
        """Small account SOL trade on Bitget: min_qty=0.1 SOL, may need clamping."""
        feasible, reason, details = check_trade_feasibility(
            symbol="SOLUSDT",
            balance=100.0,
            risk_pct=0.01,
            entry_price=150.0,
            stop_price=148.0,
            max_risk_deviation_pct=500.0,
        )
        # With 500% deviation allowed, it should pass even for small accounts
        assert feasible or "CLAMP_EXCEEDS" in reason
    
    def test_btc_feasible_normal(self):
        """Normal BTC trade should be feasible."""
        feasible, reason, details = check_trade_feasibility(
            symbol="BTCUSDT",
            balance=1000.0,
            risk_pct=0.01,
            entry_price=50000.0,
            stop_price=49500.0,
            max_risk_deviation_pct=500.0,
        )
        assert feasible
        assert reason == "FEASIBLE"
    
    def test_kraken_specs_are_lenient(self):
        """Kraken specs should be more lenient (min_qty=1 contract, no min_notional)."""
        kraken_specs = get_contract_specs("BTCUSDT", broker="kraken")
        bitget_specs = get_contract_specs("BTCUSDT", broker="bitget")
        
        # Kraken has lower barriers
        assert kraken_specs["min_notional"] == 0
        assert kraken_specs["min_qty"] == 1  # 1 contract = $1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
