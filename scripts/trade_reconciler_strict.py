import os
import sys
import time
import asyncio
import logging
from typing import Dict, Any, List

from dotenv import load_dotenv

# We can import our Async emergency classes which are already robust at fetching state.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__name__), "..")))
from scripts.emergency_kill import AsyncEmergencyBitget, AsyncEmergencyKraken
import aiohttp

# Import config for DB or local checks
from smc_futures.config import SMC_CONFIG
from smc_futures.db.session import SessionLocal
from smc_futures.db.models import Trade, SystemStatus

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [RECONCILER] - %(levelname)s - %(message)s'
)
logger = logging.getLogger("reconciler")

async def get_db_active_positions(broker: str) -> int:
    """Read the number of active trades currently assumed open by the Database/Bot"""
    try:
        from smc_futures.db.session import SessionLocal
        from smc_futures.db.models import Trade
        
        with SessionLocal() as session:
            # Note: The 'Trade' table has a 'status' field: 'open', 'closed', etc.
            open_trades = session.query(Trade).filter(
                Trade.status == 'open',
                Trade.broker == broker
            ).count()
            return open_trades
    except Exception as e:
        logger.error(f"Error reading DB: {e}")
        return -1


async def reconciliation_loop(interval_sec: int = 10):
    """
    Infinite loop running alongside the bot:
    Every `interval_sec` seconds, checks actual exchange positions vs DB assumed positions.
    If a mismatch is found -> Triggers global kill switch equivalent in the DB.
    """
    logger.info("="*60)
    logger.info(f"STARTING STRICT TRADE RECONCILIATION SERVICE (Interval: {interval_sec}s)")
    logger.info("="*60)
    
    bitget = AsyncEmergencyBitget()
    kraken = AsyncEmergencyKraken()

    if not bitget.ready and not kraken.ready:
        logger.error("No valid API credentials found. Aborting Reconciler.")
        sys.exit(1)

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                checks = []
                if bitget.ready:
                    checks.append(("bitget", bitget.get_open_state(session)))
                if kraken.ready:
                    checks.append(("kraken", kraken.get_open_state(session)))
                
                results = []
                for b_name, task in checks:
                    res = await task
                    results.append((b_name, res))
                
                mismatch_found = False
                
                for broker_name, (ord_cnt, pos_cnt) in results:
                    if pos_cnt == -1:
                        logger.warning(f"Failed to fetch exchange state for {broker_name}")
                        continue
                        
                    db_cnt = await get_db_active_positions(broker_name)
                    
                    if db_cnt == -1:
                        # Unable to read DB, skip this tick
                        continue
                        
                    if pos_cnt != db_cnt:
                        logger.critical(
                            f"🚨 MISMATCH DETECTED ({broker_name.upper()}) 🚨 "
                            f"| Exchange Pos: {pos_cnt} | DB Pos: {db_cnt}"
                        )
                        mismatch_found = True
                    else:
                        logger.debug(f"OK {broker_name.upper()} | Exchange: {pos_cnt} | DB: {db_cnt}")
                
                if mismatch_found:
                    logger.critical("Locking System due to Reconciliation Mismatch.")
                    with SessionLocal() as db_session:
                        # Find the row or create it
                        status_record = db_session.query(SystemStatus).first()
                        if status_record:
                            status_record.kill_switch = True
                            status_record.kill_reason = "RECONCILIATION_MISMATCH"
                        else:
                            status_record = SystemStatus(kill_switch=True, kill_reason="RECONCILIATION_MISMATCH")
                            db_session.add(status_record)
                        db_session.commit()
                        logger.critical("Global Kill Switch Engaged via Database.")
                    
        except Exception as e:
            logger.error(f"Reconciliation iteration failed: {e}")
            
        await asyncio.sleep(interval_sec)

def start_reconciler():
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(reconciliation_loop(interval_sec=10))
    except KeyboardInterrupt:
        logger.info("Reconciler stopped manually.")

if __name__ == "__main__":
    start_reconciler()
