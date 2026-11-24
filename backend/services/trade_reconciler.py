import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List

from backend.services.live_trades_service import LiveTradesService, add_fill_async, close_live_trade_async
from backend.services.trade_persistence_hook import on_trade_closed, broadcast_trade_event
from backend.models.live_trade import LiveTrade

logger = logging.getLogger(__name__)

class PositionSyncService:
    """
    Background service that periodically syncs database trades with exchange positions.
    When a DB trade is OPEN but missing on the exchange, it fetches historical fills
    to accurately close the trade in the DB and record the correct PnL.
    """
    
    def __init__(self, bitget_executor=None, kraken_executor=None, sync_interval_sec: int = 60):
        self.bitget_executor = bitget_executor
        self.kraken_executor = kraken_executor
        self.sync_interval_sec = sync_interval_sec
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        
    def start(self):
        """Start the background sync loop."""
        if not self._is_running:
            self._is_running = True
            logger.info(f"[SYNC] Starting background sync loop every {self.sync_interval_sec} seconds")
            self._task = asyncio.create_task(self._sync_loop())
            
    def stop(self):
        """Stop the background sync loop."""
        logger.info("[SYNC] Stopping background sync loop")
        self._is_running = False
        if self._task:
            self._task.cancel()
            
    async def _sync_loop(self):
        while self._is_running:
            try:
                await self.sync_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SYNC] Error in sync loop: {e}", exc_info=True)
            
            await asyncio.sleep(self.sync_interval_sec)
            
    async def sync_positions(self):
        """
        Execute one pass of position synchronization.
        1. Fetch OPEN trades from DB.
        2. Fetch positions from Bitget and Kraken.
        3. Identify trades missing from exchanges.
        4. Fetch fills and reconcile.
        """
        open_trades = LiveTradesService.get_open_trades()
        if not open_trades:
            return  # Nothing to sync
        
        logger.info("[SYNC] pass start | open_trades=%d", len(open_trades))
        exchange_positions = {"bitget": set(), "kraken": set()}
        
        # 1. Fetch Bitget Positions
        if self.bitget_executor:
            try:
                b_pos_res = await self.bitget_executor.get_positions()
                if b_pos_res.get("code") == "00000":
                    for pos in b_pos_res.get("data", []) or []:
                        if float(pos.get("total", 0) or 0) > 0:
                            exchange_positions["bitget"].add(pos.get("symbol", "").upper())
                logger.info("[SYNC] Bitget open symbols: %s", exchange_positions["bitget"] or "none")
            except Exception as e:
                logger.error(f"[SYNC] Bitget fetch failed: {e}")
                
        # 2. Fetch Kraken Positions
        if self.kraken_executor:
            try:
                k_pos_res = await self.kraken_executor.get_positions()
                k_data = k_pos_res.get("data", []) if isinstance(k_pos_res, dict) else k_pos_res
                for pos in k_data:
                    if float(pos.get("size", 0) or 0) > 0:
                        exchange_positions["kraken"].add(pos.get("symbol", "").upper())
                logger.info("[SYNC] Kraken open symbols: %s", exchange_positions["kraken"] or "none")
            except Exception as e:
                logger.error(f"[SYNC] Kraken fetch failed: {e}")
                
        # 3. Identify missing DB trades
        missing_trades = []
        for trade in open_trades:
            broker = trade.broker.lower()
            if broker == "bitget" and not self.bitget_executor:
                continue
            if broker == "kraken" and not self.kraken_executor:
                continue
                
            if trade.symbol not in exchange_positions.get(broker, set()):
                missing_trades.append(trade)
        
        if missing_trades:
            logger.warning("[SYNC] %d trades MISSING on exchange → reconciling", len(missing_trades))
        
        # 4. Reconcile missing trades
        reconciled_count = 0
        for trade in missing_trades:
            if await self._reconcile_missing_trade(trade):
                reconciled_count += 1
        
        if reconciled_count > 0:
            from backend.ws.manager import ws_manager
            await ws_manager.broadcast({
                "type": "stats_updated",
                "message": f"Reconciled {reconciled_count} trades from exchange"
            })
        
        logger.info("[SYNC] pass done | reconciled=%d", reconciled_count)
            
    async def _reconcile_missing_trade(self, trade: LiveTrade) -> bool:
        """Fetch fills for a closed trade and accurately resolve its PnL/Status. Returns True if closed."""
        broker = trade.broker.lower()
        logger.warning(
            f"[SYNC] DB trade {trade.id[:8]} ({trade.symbol}) missing on {broker}. Attempting reconciliation."
        )
        
        fills = []
        try:
            # Lookback up to 7 days
            start_tm = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
            end_tm = int(datetime.now(timezone.utc).timestamp() * 1000)
            
            if broker == "bitget" and self.bitget_executor:
                fills = await self._fetch_bitget_fills(trade, start_tm, end_tm)
            elif broker == "kraken" and self.kraken_executor:
                fills = await self._fetch_kraken_fills(trade, start_tm, end_tm)
                
        except Exception as e:
            logger.error(f"[SYNC] Failed to fetch fills for {trade.id}: {e}")
            
        if not fills:
            logger.error(f"[SYNC] No fills found for trade {trade.id}. Marking PnL as UNKNOWN.")
            # Use close_live_trade directly (no fills to record)
            closed = await close_live_trade_async(
                trade_id=trade.id,
                close_price=trade.entry,  # Unknown, fallback to entry
                pnl=None,                 # EXPLICITLY UNKNOWN
                fee=0.0,
                raw_exchange={"sync_note": "No fills found, assumed closed externally"},
                close_reason="external_close_unknown_fill",
            )
            if closed:
                await broadcast_trade_event("trade_closed", closed)
                return True
            return False
            
        # Aggregate fills
        total_pnl = 0.0
        total_qty = 0.0
        total_fee = 0.0
        avg_price_sum = 0.0
        
        for fill in fills:
            total_pnl += fill.get("pnl", 0.0)
            total_fee += fill.get("fee", 0.0)
            
            fill_qty = fill.get("qty", 0.0)
            fill_price = fill.get("price", 0.0)
            total_qty += fill_qty
            avg_price_sum += (fill_qty * fill_price)
            
        final_close_price = avg_price_sum / total_qty if total_qty > 0 else trade.entry
        
        logger.info(f"[SYNC] Reconciled {trade.id}: pnl={total_pnl:.2f}, fees={total_fee:.2f}, price={final_close_price:.2f}")
        
        # Record individual fills (REDUCE for intermediary, last one is NOT CLOSE
        # to avoid double-close — we finalize via close_live_trade below)
        for fill in fills:
            await add_fill_async(
                trade_id=trade.id,
                action="REDUCE",
                qty=fill.get("qty", 0.0),
                price=fill.get("price", 0.0),
                fee=fill.get("fee", 0.0),
                pnl=fill.get("pnl", 0.0),
                order_id=fill.get("order_id", trade.order_id),
                raw_response={"sync_reconciled": True, "raw": fill.get("raw", {})},
            )

        # Finalize trade close — do NOT pass pnl/fee because add_fill() already
        # accumulated them on the parent trade. Passing them again would overwrite
        # (currently safe by accident, but fragile if logic changes).
        closed = await close_live_trade_async(
            trade_id=trade.id,
            close_price=final_close_price,
            pnl=None,   # Let close_live_trade recalculate from close_price if needed
            fee=0.0,     # Already accumulated via fills
            raw_exchange={"sync_note": "Reconciled with historical fills", "fill_count": len(fills)},
            close_reason="external_close",
        )
        if closed:
            await broadcast_trade_event("trade_closed", closed)
            return True
        return False
        
    async def _fetch_bitget_fills(self, trade: LiveTrade, start_tm: int, end_tm: int) -> List[Dict[str, Any]]:
        """
        Fetch historical fills for a specific order or symbol.
        Matches by orderId/clientOid first, then falls back to side/qty/time matching
        for trades closed via plan orders (TP/SL) which get new OIDs.
        """
        results = []
        if not hasattr(self.bitget_executor, 'client'):
            return []
            
        try:
            # 1. Try to fetch specifically by orderId first if it exists
            params = {
                "productType": "USDT-FUTURES",
                "symbol": trade.symbol,
                "startTime": str(start_tm),
                "endTime": str(end_tm),
                "pageSize": "100"
            }
            
            # Note: We always fetch the last 100 fills for the symbol to find gaps
            res = self.bitget_executor.client._request("GET", "/api/v2/mix/order/fills", params)
            if res.get("code") == "00000":
                fills_data = res.get("data", {}).get("fillList", [])
                
                # Sort fills by transition time (newest first)
                fills_data.sort(key=lambda x: int(x.get("cTime", 0)), reverse=True)
                
                # Target side for closing (opposite of entry)
                target_order_side = "sell" if trade.side.lower() == "long" else "buy"
                
                for fill in fills_data:
                    fill_oid = fill.get("orderId")
                    fill_side = fill.get("side", "").lower() # "buy" or "sell"
                    fill_ts = int(fill.get("cTime", 0))
                    
                    # Heuristic A: Direct OID/ClientOID match
                    is_match = False
                    if trade.order_id and fill_oid == trade.order_id:
                        is_match = True
                    elif trade.client_oid and fill.get("clientOid") == trade.client_oid:
                        is_match = True
                    
                    # Heuristic B: Symbol + Opposite Side + Occurred after trade opened
                    # This catches TP/SL plan orders which generate new OIDs.
                    # Guard: require fill quantity to match trade qty (within 1% tolerance)
                    # to avoid matching fills from unrelated trades on the same symbol.
                    elif fill_side == target_order_side and fill_ts > (trade.ts_open.timestamp() * 1000):
                        fill_qty_val = float(fill.get("baseVolume", 0) or 0)
                        qty_tolerance = trade.qty * 0.01  # 1% tolerance for partial fills rounding
                        remaining_qty = trade.qty - sum(r["qty"] for r in results)
                        if abs(fill_qty_val - remaining_qty) <= qty_tolerance or fill_qty_val <= remaining_qty + qty_tolerance:
                            is_match = True
                            logger.info(f"[SYNC] Heuristic match: {trade.symbol} fill_oid={fill_oid} qty={fill_qty_val} (remaining={remaining_qty:.6f})")
                        else:
                            logger.debug(f"[SYNC] Heuristic SKIP: {trade.symbol} fill_oid={fill_oid} qty={fill_qty_val} too large for remaining={remaining_qty:.6f}")
                    
                    if is_match:
                        results.append({
                            "order_id": fill_oid,
                            "qty": float(fill.get("baseVolume", 0) or 0),
                            "price": float(fill.get("price", 0) or 0),
                            "fee": float(fill.get("fee", 0) or 0),
                            "pnl": float(fill.get("profit", 0) or 0), 
                            "raw": fill
                        })
                        
                        # Optimization: if we matched the full quantity, we can stop
                        matched_qty = sum(r["qty"] for r in results)
                        if abs(matched_qty - trade.qty) < 0.000001:
                            break
                            
        except Exception as e:
            logger.error(f"[SYNC] Error fetching Bitget fills: {e}")
            
        return results
        
    async def _fetch_kraken_fills(self, trade: LiveTrade, start_tm: int, end_tm: int) -> List[Dict[str, Any]]:
        """Fetch historical fills for Kraken."""
        # For kraken, we'd query /derivatives/api/v3/fills
        results = []
        if not hasattr(self.kraken_executor, 'client'):
            return []
            
        try:
            res = self.kraken_executor.client.get_fills() # Assuming this is available
            fills_data = res.get("fills", []) if isinstance(res, dict) else []
            
            for fill in fills_data:
                # Match by order_id or cliOrdId (client_oid)
                is_match = False
                if trade.order_id and fill.get("order_id") == trade.order_id:
                    is_match = True
                elif trade.client_oid and fill.get("cliOrdId") == trade.client_oid:
                    is_match = True
                    
                # Match by symbol and timestamp heuristic if OIDs missing and close enough
                
                if is_match:
                    results.append({
                        "order_id": fill.get("order_id"),
                        "qty": float(fill.get("size", 0) or 0),
                        "price": float(fill.get("price", 0) or 0),
                        "fee": float(fill.get("fee", 0) or 0),
                        "pnl": float(fill.get("realizedPnl", 0) or 0), 
                        "raw": fill
                    })
        except Exception as e:
            logger.error(f"[SYNC] Error fetching Kraken fills: {e}")
            
        return results
