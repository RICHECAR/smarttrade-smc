import asyncio
import logging
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from backend.db.session import SessionLocal
from backend.models.persisted_position import PersistedPosition
from smc_futures.app_context import build_app_context
from smc_futures.smc_config import SMC_CONFIG

logger = logging.getLogger("strict_reconciler")
logger.setLevel(logging.INFO)

class StrictTradeReconciler:
    def __init__(self):
        # We reuse app context for its executors. The reconciler runs independently.
        self.ctx = build_app_context()
        self.bitget = self.ctx.executors.get("futures")
        self.kraken = self.ctx.executors.get("kraken_futures")
        self.interval_sec = 10
        
    async def fetch_exchange_positions(self) -> Dict[str, dict]:
        """Fetch active positions directly from exchanges (Kraken primary)."""
        active_map = {}
        
        # 1. Kraken (primary)
        if self.kraken and SMC_CONFIG.get("KRAKEN_EXECUTION_ENABLED", False):
            try:
                kr_pos = await self.kraken.get_open_positions()
                for p in kr_pos:
                    if float(p.get("size", 0)) > 0:
                        sym = p.get("symbol", "").upper()
                        # Normalize Kraken symbols via centralized mapping
                        try:
                            from backend.kraken_contracts import from_kraken_symbol
                            sym = from_kraken_symbol(sym)
                        except (ImportError, ValueError):
                            pass  # Keep raw symbol if mapping unavailable
                        if sym not in active_map:
                            active_map[sym] = {"kraken": 0.0, "bitget": 0.0}
                        active_map[sym]["kraken"] += float(p.get("size", 0))
            except Exception as e:
                logger.error(f"[RECONCILIATION] Kraken fetch error: {e}")
                
        # 2. Bitget (legacy, only if executor exists)
        if self.bitget:
            try:
                bg_pos = await self.bitget.get_open_positions()
                for p in bg_pos:
                    if float(p.get("total", 0)) > 0:
                        sym = p.get("symbol", "").upper()
                        if sym not in active_map:
                            active_map[sym] = {"kraken": 0.0, "bitget": 0.0}
                        active_map[sym]["bitget"] += float(p.get("total", 0))
            except Exception as e:
                logger.error(f"[RECONCILIATION] Bitget fetch error: {e}")
                
        return active_map
        
    def fetch_db_positions(self) -> List[str]:
        """Fetch symbols with active positions according to internal DB state."""
        db = SessionLocal()
        try:
            active_pos = db.query(PersistedPosition).filter(PersistedPosition.status == "OPEN").all()
            return [p.symbol.upper() for p in active_pos]
        finally:
            db.close()
            
    async def reconcile_loop(self):
        logger.info("Starting Strict Trade Reconciler Loop (10s interval)...")
        while True:
            try:
                exchange_positions = await self.fetch_exchange_positions()
                db_symbols = self.fetch_db_positions()
                
                mismatch_found = False
                issues = []
                
                # Check DB symbols existing on exchanges
                for db_sym in db_symbols:
                    ex_data = exchange_positions.get(db_sym)
                    if not ex_data:
                        mismatch_found = True
                        issues.append(f"DB thinks {db_sym} is OPEN, but missing on exchanges.")
                    elif self.kraken and SMC_CONFIG.get("KRAKEN_EXECUTION_ENABLED", False):
                        if ex_data["bitget"] > 0 and ex_data["kraken"] == 0:
                            mismatch_found = True
                            issues.append(f"Asymmetry: {db_sym} open on Bitget but 0 on Kraken.")
                
                # Check Exchange symbols missing in DB
                for ex_sym, ex_data in exchange_positions.items():
                    if ex_sym not in db_symbols and (ex_data["bitget"] > 0 or ex_data["kraken"] > 0):
                        mismatch_found = True
                        issues.append(f"Ghost Position: {ex_sym} exists on exchange(s) but not in DB.")
                        
                if mismatch_found:
                    logger.critical(f"🚨 RECONCILIATION FATAL MISMATCH DETECTED 🚨: {issues}")
                    logger.critical("DISABLING ALL NEW EXECUTIONS TO PROTECT CAPITAL.")
                    
                    if hasattr(self.ctx, "order_manager") and self.ctx.order_manager:
                        self.ctx.order_manager.engage_kill_switch(reason="Reconciliation DB Mismatch")
                    
                    # Forcing a soft exit of the loop if you want an absolute hard stop
                    # But keeping it alive running errors is also acceptable if order manager blocked.
                    
                else:
                    logger.debug("[RECONCILIATION] State is fully reconciled.")
                    
            except Exception as e:
                logger.error(f"[RECONCILIATION] Unexpected loop exception: {e}")
            
            await asyncio.sleep(self.interval_sec)

if __name__ == "__main__":
    import base64
    from smc_futures.security.secrets import init_secrets
    init_secrets()
    reconciler = StrictTradeReconciler()
    asyncio.run(reconciler.reconcile_loop())
