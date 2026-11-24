"""
SMC Futures Orchestrator with Ollama Integration
Coordinates all modules + DeepSeek Ollama validation
"""

import asyncio
import json
import os
import logging
import sqlite3
import time
import httpx
from datetime import datetime, timezone
from typing import Dict, Optional, List, Any
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Header, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import traceback

from smc_futures.engine.strategy import (
    StructureEngine, LiquidityEngine, OrderBlockEngine, FVGEngine,
    MTFBiasEngine, SignalBuilder, Candle
)
from smc_futures.engine.risk import FuturesRiskEngine, TradeValidator
from smc_futures.engine.execution import BitgetFuturesExecutor
from smc_futures.ollama_client import analyze_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SMCFuturesOrchestratorWithOllama:
    """
    Complete SMC Futures Trading Engine with Ollama DeepSeek analysis
    """
    
    def __init__(
        self,
        symbol: str,
        account_balance: float = 10000.0,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        passphrase: Optional[str] = None,
        live_trading: bool = False,
        use_ollama: bool = True
    ):
        self.symbol = symbol
        self.account_balance = account_balance
        self.live_trading = live_trading
        self.use_ollama = use_ollama
        
        # SMC Detection Modules
        self.structure_engine = StructureEngine(lookback_period=100)
        self.liquidity_engine = LiquidityEngine()
        self.ob_engine = OrderBlockEngine()
        self.fvg_engine = FVGEngine()
        self.mtf_engine = MTFBiasEngine()
        self.signal_builder = SignalBuilder(min_rr_ratio=1.5)
        
        # Risk Management
        self.risk_engine = FuturesRiskEngine(
            account_balance=account_balance,
            risk_per_trade_percent=1.0,
            max_leverage=10.0
        )
        self.validator = TradeValidator(self.risk_engine)
        
        # Execution (if live trading)
        self.executor = None
        if live_trading and api_key and secret_key and passphrase:
            self.executor = BitgetFuturesExecutor(api_key, secret_key, passphrase)
        
        # State tracking
        self.last_signal: Optional[Dict] = None
        self.active_trade: Optional[Dict] = None
        self.signal_history: List[Dict] = []
        self.trade_log: List[Dict] = []
        self.last_candle: Optional[Dict] = None
    
    def _build_ollama_prompt(self, candle: Dict, signal: Dict) -> str:
        """Build a prompt for Ollama to validate/analyze the signal"""
        prompt = f"""
You are a professional SMC (Smart Money Concept) trading analyst using DeepSeek AI.

CURRENT MARKET DATA:
- Symbol: {self.symbol}
- Current Price: ${candle['close']:.2f}
- Current Candle: Open=${candle['open']:.2f}, High=${candle['high']:.2f}, Low={candle['low']:.2f}, Close=${candle['close']:.2f}

GENERATED SMC SIGNAL:
{json.dumps(signal, indent=2, default=str)}

ANALYSIS REQUESTED:
1. Is this signal valid based on SMC institutional order block + fair value gap theory?
2. What is the probability of success (0-100)?
3. Any additional price levels to watch?
4. Risk assessment and recommendation (CONFIRM / REVISE / REJECT)

Please respond in JSON format:
{{
  "valid": true/false,
  "confidence": 0-100,
  "recommendation": "CONFIRM/REVISE/REJECT",
  "analysis": "brief explanation",
  "key_levels": {{"resistance": 0.0, "support": 0.0}}
}}
"""
        return prompt.strip()
    
    async def _validate_with_ollama(self, signal: Dict, candle: Dict) -> Dict:
        """Validate signal using local Ollama DeepSeek"""
        try:
            prompt = self._build_ollama_prompt(candle, signal)
            # Use to_thread because analyze_text is blocking (subprocess)
            response = await asyncio.to_thread(analyze_text, prompt, 30)
            
            # Try to extract JSON from response
            if response.startswith("OLLAMA_"):
                logger.warning(f"⚠️  Ollama error: {response}")
                return {"valid": True, "confidence": 50, "recommendation": "CONFIRM", "note": "Ollama unavailable, proceeding with caution"}
            
            # Parse JSON response
            try:
                # Find JSON block in response
                start_idx = response.find('{')
                end_idx = response.rfind('}') + 1
                if start_idx >= 0 and end_idx > start_idx:
                    json_str = response[start_idx:end_idx]
                    analysis = json.loads(json_str)
                    logger.info(f"✅ Ollama Analysis: {analysis.get('recommendation', 'UNKNOWN')}")
                    return analysis
                else:
                    logger.warning(f"❌ Could not parse JSON from Ollama response")
                    return {"valid": True, "confidence": 50, "recommendation": "CONFIRM"}
            except json.JSONDecodeError as e:
                logger.warning(f"❌ JSON parse error: {e}")
                return {"valid": True, "confidence": 50, "recommendation": "CONFIRM"}
        
        except Exception as e:
            logger.error(f"❌ Ollama validation error: {e}")
            return {"valid": True, "confidence": 50, "recommendation": "CONFIRM"}
    
    async def process_candle(self, candle_data: Dict) -> Optional[Dict]:
        """
        Main entry point for each new candle
        
        Args:
            candle_data: {timestamp, open, high, low, close, volume}
        
        Returns:
            Signal dict or None
        """
        
        try:
            # Create Candle object
            candle = Candle(
                timestamp=candle_data.get("timestamp"),
                open=candle_data.get("open"),
                high=candle_data.get("high"),
                low=candle_data.get("low"),
                close=candle_data.get("close"),
                volume=candle_data.get("volume", 0)
            )
            self.last_candle = candle_data
            
            # Step 1: Update all SMC detection engines
            self.structure_engine.add_candle(candle)
            self.liquidity_engine.add_candle(candle)
            self.ob_engine.add_candle(candle)
            self.fvg_engine.add_candle(candle)
            
            # Step 2: Get current state from all engines
            structure_state = self.structure_engine.get_current_state()
            liquidity_state = self.liquidity_engine.get_current_state()
            ob_state = self.ob_engine.get_current_state()
            fvg_state = self.fvg_engine.get_current_state()
            
            # Step 3: Update MTF analysis
            self.mtf_engine.update_htf_data(structure_state)
            self.mtf_engine.update_ob_data("4H", ob_state)
            self.mtf_engine.update_fvg_data("4H", fvg_state)
            mtf_state = self.mtf_engine.get_current_state()
            
            # Step 4: Build signal
            signal = self.signal_builder.build_signal(
                structure_state=structure_state,
                liquidity_state=liquidity_state,
                ob_state=ob_state,
                fvg_state=fvg_state,
                mtf_state=mtf_state,
                current_price=candle.close,
                timestamp=candle.timestamp
            )
            
            # Step 5: If signal generated, validate with Ollama + risk
            if signal:
                self.last_signal = self.signal_builder.get_last_signal()
                
                # (A) Validate with Ollama if enabled
                ollama_analysis = None
                if self.use_ollama:
                    logger.info("🤖 Sending signal to Ollama DeepSeek for validation...")
                    ollama_analysis = await self._validate_with_ollama(self.last_signal, candle_data)
                    
                    # If Ollama rejects, skip execution
                    if ollama_analysis.get("recommendation") == "REJECT":
                        logger.warning(f"❌ Ollama rejected signal: {ollama_analysis.get('analysis', '')}")
                        return None
                
                # (B) Validate signal with risk engine
                validation = self.validator.validate_signal(
                    signal=self.last_signal,
                    current_price=candle.close,
                    symbol=self.symbol,
                    leverage=5
                )
                
                if validation["valid"]:
                    # Execute trade if live trading enabled
                    if self.live_trading and self.executor:
                        exec_result = await self.executor.execute_smc_trade(
                            symbol=self.symbol,
                            signal=self.last_signal,
                            quantity=validation["position_size"]["quantity"],
                            leverage=5
                        )
                        
                        if exec_result.get("success"):
                            logger.info(f"✅ Trade executed: {self.last_signal}")
                            self.active_trade = self.last_signal.copy()
                            self.trade_log.append({
                                "timestamp": datetime.now().isoformat(),
                                "signal": self.last_signal,
                                "ollama_analysis": ollama_analysis,
                                "execution": exec_result
                            })
                    else:
                        logger.info(f"📊 Signal generated (SIMULATION): {self.last_signal}")
                        if ollama_analysis:
                            logger.info(f"   Ollama confidence: {ollama_analysis.get('confidence', 'N/A')}%")
                    
                    self.signal_history.append(self.last_signal)
                    
                    # Cap history to prevent memory leak
                    if len(self.signal_history) > 100:
                        self.signal_history.pop(0)
                    if len(self.trade_log) > 100:
                        self.trade_log.pop(0)

                    return self.last_signal
                else:
                    logger.warning(f"❌ Signal validation failed: {validation.get('errors', [])}")
            
            return None
        
        except Exception as e:
            logger.error(f"❌ Error processing candle: {e}")
            return None
    
    def get_dashboard_state(self) -> Dict:
        """Get current state for dashboard visualization"""
        return {
            "symbol": self.symbol,
            "ollama_enabled": self.use_ollama,
            "last_candle": self.last_candle,
            "account": self.risk_engine.get_account_state(),
            "last_signal": self.last_signal,
            "active_trade": self.active_trade,
            "structure": self.structure_engine.get_current_state(),
            "liquidity": self.liquidity_engine.get_current_state(),
            "order_blocks": self.ob_engine.get_current_state(),
            "fvgs": self.fvg_engine.get_current_state(),
            "mtf_bias": self.mtf_engine.get_current_state(),
            "signal_count": len(self.signal_history)
        }


app = FastAPI(title="SMC Futures Engine (with Ollama)", version="2.0")

_ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173,http://localhost:8000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)

from backend.auth import ApiKeyMiddleware
app.add_middleware(ApiKeyMiddleware)

class CandleInput(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

class StrategyConfig(BaseModel):
    symbol: str
    account_balance: float = 0.0  # Safe default: 0.0
    live_trading: bool = False
    use_ollama: bool = True  # Enable DeepSeek validation

# Shared global executors
futures_executor = BitgetFuturesExecutor(
    api_key=os.getenv("BITGET_API_KEY", ""),
    secret_key=os.getenv("BITGET_API_SECRET", ""),
    passphrase=os.getenv("BITGET_PASSPHRASE", "")
)

# Global orchestrator instances by symbol
orchestrators: Dict[str, SMCFuturesOrchestratorWithOllama] = {}

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# --- Background Market Processor ---
async def strategy_loop():
    """ 
    Proactive Strategy Loop: 
    Fetches market data and processes signals in background.
    """
    logger.info("🚀 Starting Ollama Strategy Loop...")
    while True:
        try:
            for symbol, orch in list(orchestrators.items()):
                # Fetch data and process
                raw_candles = await get_candles(symbol=symbol, tf="4h", limit=100)
                if raw_candles:
                    candle = raw_candles[-1]
                    candle['timestamp'] = candle['ts']
                    signal = await orch.process_candle(candle)
                    
                    state = orch.get_dashboard_state()
                    await manager.broadcast({
                        "type": "update",
                        "symbol": symbol,
                        "state": state
                    })
                    
                    if signal:
                        await manager.broadcast({
                            "type": "signal",
                            "symbol": symbol,
                            "signal": signal,
                            "timestamp": datetime.now().isoformat()
                        })
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"❌ Strategy Loop Error: {e}")
            await asyncio.sleep(10)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(strategy_loop())

@app.post("/api/smc/init")
async def initialize_strategy(config: StrategyConfig):
    """Initialize SMC strategy for a specific symbol"""
    global orchestrators
    
    try:
        clean_symbol = normalize_symbol(config.symbol)
        orch = SMCFuturesOrchestratorWithOllama(
            symbol=clean_symbol,
            account_balance=config.account_balance,
            live_trading=config.live_trading,
            use_ollama=config.use_ollama
        )
        orchestrators[clean_symbol] = orch
        logger.info(f"🚀 Initialized SMC Engine for {clean_symbol}")
        
        # Trigger immediate historical data fetch to avoid "Wait for next candle"
        initial_candles = []
        try:
            initial_candles = await get_candles(symbol=clean_symbol, tf="4h", limit=100)
            if initial_candles:
                logger.info(f"📚 Pre-loading {len(initial_candles)} candles for {clean_symbol}")
                for c in initial_candles:
                    c['timestamp'] = c['ts']
                    await orch.process_candle(c)
        except Exception as e:
            logger.warning(f"⚠️ Could not pre-load candles for {clean_symbol}: {e}")

        return {
            "status": "initialized",
            "symbol": clean_symbol,
            "balance": config.account_balance,
            "ollama_enabled": config.use_ollama,
            "candles_loaded": len(initial_candles)
        }
    except Exception as e:
        logger.error(f"❌ Initialization failed for {config.symbol}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/smc/process-candle")
async def process_candle(candle: CandleInput, symbol: str = Query(...)):
    """Process new candle for a specific symbol"""
    clean_symbol = normalize_symbol(symbol)
    engine = orchestrators.get(clean_symbol)
    
    if not engine:
        raise HTTPException(status_code=400, detail=f"Engine not initialized for {clean_symbol}")
    
    try:
        signal = await engine.process_candle(candle.dict())
        return {
            "signal": signal,
            "state": engine.get_dashboard_state()
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/smc/state")
async def get_state(symbol: str = Query(...)):
    """Get current strategy state for a specific symbol"""
    try:
        clean_symbol = normalize_symbol(symbol)
        engine = orchestrators.get(clean_symbol)
        
        if not engine:
            return JSONResponse(
                status_code=404,
                content={"error": "not_initialized", "symbol": clean_symbol, "message": f"Engine not initialized for {clean_symbol}"}
            )
        
        return engine.get_dashboard_state()
    except Exception as e:
        logger.error(f"Error in get_state: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "route": "/api/smc/state",
                "message": str(e)
            }
        )

# --- V1 Account/Trading Endpoints (Synced from orchestrator.py) ---

@app.get("/api/v1/account/futures")
async def get_futures_balance(
    marginCoin: str = "USDT",
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    x_api_secret: Optional[str] = Header(None, alias="X-API-SECRET"),
    x_api_passphrase: Optional[str] = Header(None, alias="X-API-PASSPHRASE")
):
    """Get futures account balance"""
    try:
        executor = futures_executor
        if x_api_key and x_api_secret and x_api_passphrase:
            executor = BitgetFuturesExecutor(x_api_key, x_api_secret, x_api_passphrase)
        
        balance = await executor.get_account_balance(marginCoin)
        return balance
    except Exception as e:
        logger.error(f"Error in get_futures_balance: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "route": "/api/v1/account/futures",
                "message": str(e),
                "traceback": traceback.format_exc() if os.getenv("DEBUG") else None
            }
        )

@app.get("/api/v1/positions")
async def get_positions(
    symbol: Optional[str] = None,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    x_api_secret: Optional[str] = Header(None, alias="X-API-SECRET"),
    x_api_passphrase: Optional[str] = Header(None, alias="X-API-PASSPHRASE")
):
    """Get open positions"""
    try:
        executor = futures_executor
        if x_api_key and x_api_secret and x_api_passphrase:
            executor = BitgetFuturesExecutor(x_api_key, x_api_secret, x_api_passphrase)
        
        # Use get_positions with fallback
        if hasattr(executor, "get_positions"):
            positions = await executor.get_positions(symbol)
        elif hasattr(executor, "get_open_positions"):
            positions = await executor.get_open_positions(symbol)
        else:
            return JSONResponse(status_code=501, content={"error": "not_implemented"})
        return positions
    except Exception as e:
        logger.error(f"Error in get_positions: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "route": "/api/v1/positions",
                "message": str(e)
            }
        )

@app.get("/api/v1/trades/history")
async def get_trade_history(
    symbol: str, 
    limit: int = 50,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    x_api_secret: Optional[str] = Header(None, alias="X-API-SECRET"),
    x_api_passphrase: Optional[str] = Header(None, alias="X-API-PASSPHRASE")
):
    """Get past fills"""
    try:
        executor = futures_executor
        if x_api_key and x_api_secret and x_api_passphrase:
            executor = BitgetFuturesExecutor(x_api_key, x_api_secret, x_api_passphrase)
        
        history = await executor.get_trade_history(symbol, limit)
        return history
    except Exception as e:
        logger.error(f"Error in get_trade_history: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "route": "/api/v1/trades/history",
                "message": str(e)
            }
        )

class OrderRequest(BaseModel):
    symbol: str
    side: str
    orderType: str
    quantity: float
    price: Optional[float] = None
    stopLoss: Optional[float] = None
    takeProfit: Optional[float] = None
    posSide: Optional[str] = None
    tradeSide: Optional[str] = 'open'
    productType: Optional[str] = 'USDT-FUTURES'

@app.post("/api/v1/order")
async def place_order(
    req: OrderRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    x_api_secret: Optional[str] = Header(None, alias="X-API-SECRET"),
    x_api_passphrase: Optional[str] = Header(None, alias="X-API-PASSPHRASE")
):
    """Place an order"""
    try:
        executor = futures_executor
        if x_api_key and x_api_secret and x_api_passphrase:
            executor = BitgetFuturesExecutor(x_api_key, x_api_secret, x_api_passphrase)
        
        result = await executor.place_order(
            symbol=req.symbol,
            side=req.side,
            order_type=req.orderType,
            quantity=req.quantity,
            price=req.price,
            stop_loss=req.stopLoss,
            take_profit=req.takeProfit,
            productType=req.productType,
            posSide=req.posSide,
            marginMode="isolated", # Force isolated for safety or usage req.marginMode if added to model
            tradeSide=req.tradeSide
        )
        return result
    except Exception as e:
        logger.error(f"💥 Order endpoint failure: {traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "route": "/api/v1/order",
                "message": str(e)
            }
        )

# --- WebSocket & Market Data ---

@app.websocket("/ws/trading")
async def websocket_trading(websocket: WebSocket):
    try:
        await websocket.accept()
        await manager.connect(websocket)
        logger.info("✅ WebSocket connected")
        await websocket.send_json({"type": "ping", "message": "connected", "timestamp": datetime.now().isoformat()})
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong", "timestamp": datetime.now().isoformat()})
    except WebSocketDisconnect:
        logger.info("🔌 WebSocket client disconnected")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)

@app.get("/api/v1/market/ticker")
async def get_ticker(symbol: str, productType: str = "USDT-FUTURES"):
    try:
        url = f"{BITGET_BASE}/api/v2/mix/market/ticker"
        params = {"symbol": symbol, "productType": productType}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/smc/signals")
async def get_signals(symbol: str = Query(...), limit: int = 10):
    """Get recent signals for a specific symbol"""
    clean_symbol = normalize_symbol(symbol)
    engine = orchestrators.get(clean_symbol)
    
    if not engine:
        raise HTTPException(status_code=400, detail=f"Engine not initialized for {clean_symbol}")
    
    return {
        "total": len(engine.signal_history),
        "recent": engine.signal_history[-limit:]
    }


# --- Robust Candle Endpoint (Direct Bitget Mix API) ---

BITGET_BASE = "https://api.bitget.com"

# Mapping timeframe front -> Bitget V2 granularity
TF_MAP_V2 = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
}

def normalize_symbol(symbol: str) -> str:
    """Normalize BTC/USDT or BTC:USDT to BTCUSDT"""
    return symbol.replace("/", "").replace(":", "").replace("-", "").upper()

def to_mix_symbol(symbol: str) -> str:
    # Front: "BTC/USDT" -> Bitget mix: "BTCUSDT_UMCBL" (USDT perpetual)
    s = symbol.replace("/", "").upper()
    return f"{s}_UMCBL"

def tf_to_ms(tf: str) -> int:
    # convertit "5m" "15m" "1h" "4h" "1d" en millisecondes
    if tf.endswith("m"):
        return int(tf[:-1]) * 60_000
    if tf.endswith("h"):
        return int(tf[:-1]) * 3_600_000
    if tf.endswith("d"):
        return int(tf[:-1]) * 86_400_000
    raise ValueError("Unsupported tf")

DB_PATH = os.path.join(os.getcwd(), "market_data", "market_data.sqlite3")

@app.get("/api/smc/market/candles")
async def get_candles(
    symbol: str = Query(...),
    tf: str = Query("5m"),
    limit: int = Query(200, ge=10, le=200),
):
    # --- 1. Validate Timeframe ---
    if tf not in TF_MAP_V2:
        matched_tf = next((k for k in TF_MAP_V2.keys() if k.lower() == tf.lower()), None)
        if matched_tf:
            tf = matched_tf
        else:
            logger.warning(f"⚠️ Unsupported tf requested: {tf}")

    # --- 2. Smart Cache Strategy (Read-Through + Write-Through) ---
    clean_symbol = normalize_symbol(symbol)
    db_data = []
    need_fetch = True
    
    # Check Freshness
    if os.path.exists(DB_PATH):
        try:
            def db_check():
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("SELECT MAX(ts) FROM candles WHERE symbol=? AND timeframe=?", (clean_symbol, tf))
                row = cur.fetchone()
                max_ts = row[0] if row else None
                
                data = None
                fetch_required = True
                
                if max_ts:
                    now_ms = int(time.time() * 1000)
                    interval_ms = tf_to_ms(tf)
                    if (now_ms - max_ts) < (interval_ms * 2):
                        fetch_required = False
                
                if not fetch_required:
                    cur.execute("""
                        SELECT ts, open, high, low, close, volume
                        FROM candles
                        WHERE symbol=? AND timeframe=?
                        ORDER BY ts DESC
                        LIMIT ?
                    """, (clean_symbol, tf, limit))
                    rows = cur.fetchall()
                    if rows:
                        rows.reverse()
                        data = [
                            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
                            for r in rows
                        ]
                conn.close()
                return fetch_required, data

            need_fetch, db_data = await asyncio.to_thread(db_check)
            
            if not need_fetch and db_data:
                return db_data

        except Exception as e:
            logger.warning(f"⚠️ SQLite Read Error: {e}")

    # --- 3. Live Fetch & Store (Write-Through) ---
    logger.info(f"🌐 Fetching live async candles for {symbol} ({tf})")
    
    url = f"{BITGET_BASE}/api/v2/mix/market/candles"
    params = {
        "symbol": clean_symbol,
        "productType": "usdt-futures",
        "granularity": TF_MAP_V2.get(tf, tf),
        "limit": str(limit)
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
            
        if r.status_code != 200:
            logger.error(f"Bitget API Error: {r.status_code} {r.text}")
            raise HTTPException(status_code=502, detail=f"Bitget error: {r.text}")

        data = r.json()
        if data.get("code") != "00000":
            logger.error(f"Bitget API Logic Error: {data}")
            raise HTTPException(status_code=502, detail=f"Bitget: {data.get('msg')}")

        candles_data = data.get("data", [])
        out = []
        db_rows = []
        
        for row in candles_data:
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            v = float(row[5]) if len(row) > 5 else 0.0
            out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
            db_rows.append(("bitget", clean_symbol, tf, ts, o, h, l, c, v))
        
        out.sort(key=lambda x: x["ts"])
        
        # --- 4. UPSERT into SQLite (Async Thread) ---
        if db_rows:
            def db_upsert():
                conn = sqlite3.connect(DB_PATH)
                conn.executemany("""
                    INSERT OR REPLACE INTO candles(exchange, symbol, timeframe, ts, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, db_rows)
                conn.commit()
                conn.close()
            await asyncio.to_thread(db_upsert)
            logger.info(f"💾 Cached {len(db_rows)} candles for {symbol}")

        return out

    except Exception as e:
        logger.error(f"❌ Failed to fetch candles for {symbol}: {e}")
        return []

@app.get("/api/smc/health")
async def health():
    """Health check"""
    return {
        "status": "healthy",
        "orchestrator": "initialized" if 'orchestrator' in globals() and orchestrator else "not initialized",
        "timestamp": datetime.now().isoformat(),
        "local_data_path": os.path.join(os.getcwd(), "market_data", "candles")
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
