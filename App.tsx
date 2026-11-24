import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Settings, BarChart2, Activity, Zap, RefreshCw, AlertTriangle, ArrowLeft, Loader2, Wallet, Clock, DollarSign, BrainCircuit, LineChart, FileText, PieChart, Database } from 'lucide-react';
import { DEFAULT_CONFIG } from './constants';
import { KrakenService } from './services/krakenService';
import { analyzeMarket, generateSignal, getSmcCommentary } from './services/smcStrategy';
import { Candle, MarketStructure, TradeSignal, Trend, ApiConfig, Position } from './types';
import ChartVisualizer from './components/ChartVisualizer';
import SettingsView from './components/SettingsView';
import { useBotWebSocket } from './hooks/useBotWebSocket';
import Dashboard from './pages/Dashboard';
import SystemGovernance from './pages/SystemGovernance';
import DataExplorer from './pages/DataExplorer';

const App: React.FC = () => {
    const [selectedSymbol, setSelectedSymbol] = useState(DEFAULT_CONFIG.symbols[0]);
    const [currentPrice, setCurrentPrice] = useState<number>(0);
    const [candles, setCandles] = useState<Candle[]>([]);
    const [htfAnalysis, setHtfAnalysis] = useState<MarketStructure | null>(null);
    const [ltfAnalysis, setLtfAnalysis] = useState<MarketStructure | null>(null);
    const [signals, setSignals] = useState<TradeSignal[]>([]);
    const [positions, setPositions] = useState<Position[]>([]);
    const [lastAnalysisTime, setLastAnalysisTime] = useState<Date | null>(null);
    const [analysisCommentary, setAnalysisCommentary] = useState<string>("Initializing analysis...");
    const [loading, setLoading] = useState(false);
    const [executingSignalId, setExecutingSignalId] = useState<number | null>(null);
    const [activeTab, setActiveTab] = useState<'DASHBOARD' | 'SETTINGS' | 'STATS' | 'GOVERNANCE' | 'ANALYTICS'>('DASHBOARD');

    // Mobile Tab State (Chart | Analysis | Signals)
    const [mobileTab, setMobileTab] = useState<'CHART' | 'ANALYSIS' | 'SIGNALS'>('CHART');
    const [backendStatus, setBackendStatus] = useState<'CONNECTED' | 'DISCONNECTED' | 'CONNECTING'>('CONNECTING');
    const [wrongBackend, setWrongBackend] = useState(false);
    const [executionHealth, setExecutionHealth] = useState<{ execution_mode: string; cooldown_active: boolean } | null>(null);

    // ?? Real-time WebSocket from Python backend (no polling needed)
    const { connected: wsConnected, latestSignal: wsSignal, botState } = useBotWebSocket();

    // Health check on mount: verify we're talking to smc-trader, not SmartTrade AI
    useEffect(() => {
        const check = async () => {
            const health = await KrakenService.checkHealth();
            if (health.wrongBackend) {
                setWrongBackend(true);
                setBackendStatus('DISCONNECTED');
                console.error(`[HEALTH] Wrong backend: service=${health.service} (expected smc-trader)`);
            } else if (health.ok) {
                setWrongBackend(false);
                setBackendStatus('CONNECTED');
            } else {
                setBackendStatus('DISCONNECTED');
            }

            // Fetch Execution Health
            try {
                const exHealth = await KrakenService.getExecutionHealth();
                setExecutionHealth(exHealth);
            } catch (err) {
                console.error("[HEALTH] Failed to fetch execution health", err);
            }
        };
        check();
        const interval = setInterval(check, 30000);
        return () => clearInterval(interval);
    }, []);

    // Auto-update backend status from WebSocket connection
    useEffect(() => {
        if (!wrongBackend) {
            setBackendStatus(wsConnected ? 'CONNECTED' : 'DISCONNECTED');
        }
    }, [wsConnected, wrongBackend]);

    // Auto-push signals received from backend WebSocket
    useEffect(() => {
        if (wsSignal) {
            setSignals(prev => {
                // Duplicate check
                if (prev.length > 0 && prev[0].symbol === wsSignal.symbol && prev[0].type === wsSignal.direction && (Date.now() - prev[0].timestamp < 60000)) {
                    return prev;
                }
                const newSignal: TradeSignal = {
                    symbol: wsSignal.symbol || selectedSymbol,
                    type: wsSignal.direction === 'short' ? 'SHORT' : 'LONG',
                    entryPrice: wsSignal.entry || wsSignal.current_price || 0,
                    stopLoss: wsSignal.stop_loss || 0,
                    tp1: wsSignal.take_profit || wsSignal.tp1 || 0,
                    tp2: wsSignal.tp2 || 0,
                    confidence: wsSignal.confidence >= 0.8 ? 'HIGH' : wsSignal.confidence >= 0.5 ? 'MEDIUM' : 'LOW',
                    timestamp: Date.now(),
                    reason: Array.isArray(wsSignal.reason) ? wsSignal.reason : [wsSignal.reason || 'SMC Signal (Backend)'],
                };
                return [newSignal, ...prev.slice(0, 49)];
            });
        }
    }, [wsSignal, selectedSymbol]);

    // API Config State (security: NEVER persist exchange API keys/secrets in browser localStorage)
    const [apiConfig, setApiConfig] = useState<ApiConfig>(() => {
        try {
            const saved = localStorage.getItem('kraken_smc_config');
            if (saved) {
                const parsed = JSON.parse(saved);
                return {
                    apiKey: '',
                    secretKey: '',
                    passphrase: '',
                    liveMode: Boolean(parsed?.liveMode),
                };
            }
        } catch {
            try { localStorage.removeItem('kraken_smc_config'); } catch { /* ignore */ }
        }
        return { apiKey: '', secretKey: '', passphrase: '', liveMode: false };
    });

    const handleSaveConfig = (config: ApiConfig) => {
        setApiConfig(config);
        try {
            // Persist non-sensitive UI preference only; API credentials live exclusively in backend .env
            localStorage.setItem('kraken_smc_config', JSON.stringify({ liveMode: Boolean(config.liveMode) }));
        } catch { /* ignore storage quota errors */ }
        setActiveTab('DASHBOARD');
    };

    const tickerInFlight = useRef(false);
    const analysisInFlight = useRef(false);

    // 1. FAST Polling: Ticker Price (Every 2 seconds for Real-Time feel)
    useEffect(() => {
        const fetchTicker = async () => {
            if (tickerInFlight.current) return;
            tickerInFlight.current = true;
            try {
                const ticker = await KrakenService.getTicker(selectedSymbol);
                if (ticker) {
                    // Use markPrice if available, fallback to lastPr
                    const price = parseFloat(ticker.markPrice || ticker.lastPr || '0');
                    if (price > 0) setCurrentPrice(price);
                    if (!wrongBackend) {
                        setBackendStatus(ticker.isReal ? 'CONNECTED' : 'DISCONNECTED');
                    }
                } else if (!wrongBackend) {
                    setBackendStatus('DISCONNECTED');
                }
            } catch (e) {
                if (!wrongBackend) setBackendStatus('DISCONNECTED');
            } finally {
                tickerInFlight.current = false;
            }
        };

        fetchTicker();
        const interval = setInterval(fetchTicker, 30000); // 30 seconds
        return () => clearInterval(interval);
    }, [selectedSymbol]);

    // 2. ANALYSIS Polling: Run Strategy (Every 30 seconds)
    // We run this frequently to catch price moving into Order Blocks immediately
    const runAnalysis = useCallback(async () => {
        if (analysisInFlight.current) return;
        analysisInFlight.current = true;
        setLoading(true);
        try {
            // Fetch HTF Data (4H) for Bias - Less frequent check could be done but for now we slow entire analysis
            const htfData = await KrakenService.getCandles(selectedSymbol, DEFAULT_CONFIG.timeframes.htf, 200);
            const htfRes = analyzeMarket(htfData, { pivotLen: DEFAULT_CONFIG.pivotLen });
            setHtfAnalysis(htfRes);

            // Fetch LTF Data (15m) for Execution
            const ltfData = await KrakenService.getCandles(selectedSymbol, DEFAULT_CONFIG.timeframes.ltf, 200);
            const ltfRes = analyzeMarket(ltfData, { pivotLen: 3 }); // Tighter pivots for LTF
            setCandles(ltfData);
            setLtfAnalysis(ltfRes);

            // Price reference
            const priceForAnalysis = ltfData.length > 0 ? ltfData[ltfData.length - 1].close : currentPrice;

            // Generate Commentary
            const commentary = getSmcCommentary(htfRes, ltfRes, priceForAnalysis);
            setAnalysisCommentary(commentary);

            // Generate Signals
            const newSignal = generateSignal(selectedSymbol, htfRes, ltfRes, priceForAnalysis);

            if (newSignal) {
                setSignals(prev => {
                    // Duplicate Check: Don't spam the exact same signal type within 60 seconds
                    if (prev.length > 0 && prev[0].type === newSignal.type && prev[0].symbol === newSignal.symbol && (Date.now() - prev[0].timestamp < 60000)) {
                        return prev;
                    }
                    return [newSignal, ...prev.slice(0, 49)];
                });
            }

            setLastAnalysisTime(new Date());
        } catch (e) {
            console.error(e);
            setAnalysisCommentary("Error performing analysis. Check connection.");
        } finally {
            setLoading(false);
            analysisInFlight.current = false;
        }
    }, [selectedSymbol, currentPrice]); // Removed apiConfig dependency if not needed for analysis

    // Initial load & Interval for analysis
    useEffect(() => {
        runAnalysis();
        const interval = setInterval(runAnalysis, 60000); // 60 seconds
        return () => clearInterval(interval);
    }, [runAnalysis]);

    // 3. POSITIONS Polling: Only if configured (Every 10 seconds)
    useEffect(() => {
        const hasKeys = apiConfig.apiKey && apiConfig.secretKey;
        if (!hasKeys) {
            setPositions([]);
            return;
        }

        const fetchPositions = async () => {
            try {
                const pos = await KrakenService.getPositions(apiConfig);
                setPositions(pos);
            } catch (e) {
                console.error("Failed to fetch positions", e);
            }
        };

        fetchPositions();
        const interval = setInterval(fetchPositions, 60000); // 60 seconds
        return () => clearInterval(interval);
    }, [apiConfig.apiKey, apiConfig.secretKey, apiConfig.passphrase]); // Only reset loop if credentials change

    const handleExecuteSignal = async (signal: TradeSignal) => {
        if (apiConfig.liveMode) {
            if (!apiConfig.apiKey || !apiConfig.secretKey) {
                alert("Missing API Credentials for Live Trading");
                return;
            }

            // 1. Pre-flight check via backend (validates qty, position, lock, etc.)
            setExecutingSignalId(signal.timestamp);

            try {
                const preflight = await KrakenService.manualPreflight(signal, apiConfig);

                if (!preflight.can_execute) {
                    // Show detailed rejection reason with fallback for undefined
                    const reason = preflight.reason || 'Unknown validation error';
                    let errorMsg = `Cannot execute: ${reason}`;

                    if (reason === 'POSITION_ALREADY_OPEN') {
                        errorMsg = `Position already open for ${signal.symbol}. Close it first.`;
                    } else if (reason === 'EXECUTION_IN_PROGRESS') {
                        errorMsg = `Another execution is in progress for ${signal.symbol}. Please wait.`;
                    } else if (reason === 'MANUAL_TRADES_DISABLED') {
                        errorMsg = 'Manual trades are disabled in config (ALLOW_MANUAL_TRADES=0)';
                    } else if (reason === 'ZERO_BALANCE') {
                        errorMsg = 'Account balance is zero or unavailable.';
                    } else if (reason.includes('min_qty') || reason.includes('MIN_QTY') || reason.includes('raw_qty')) {
                        // Parse raw_qty from reason if available
                        const qtyMatch = reason.match(/raw_qty[_\s]+([\d.]+)/);
                        const computedQty = qtyMatch ? parseFloat(qtyMatch[1]) : preflight.computed_qty;
                        const deviation = preflight.risk_deviation_pct ? ` (risk deviation ${preflight.risk_deviation_pct.toFixed(1)}%)` : '';
                        errorMsg = `Quantity too small${deviation}.\nComputed: ${computedQty?.toFixed(6)}\nMin Required: ${preflight.min_qty}`;
                    } else if (reason.includes('notional') || reason.includes('NOTIONAL')) {
                        errorMsg = `Notional too small.\nComputed: $${preflight.notional?.toFixed(2)}\nMin Required: $${preflight.min_notional}`;
                    } else if (reason.includes('RISK_DEVIATION') || reason.includes('risk_deviation')) {
                        errorMsg = `Risk deviation too high: ${preflight.risk_deviation_pct?.toFixed(1)}% (max 50%)`;
                    }

                    alert(`PRE-FLIGHT FAILED:\n${errorMsg}`);
                    setExecutingSignalId(null);
                    return;
                }

                // 2. Confirm with user showing computed qty
                const confirmMsg = `Execute LIVE ${signal.type} on ${signal.symbol}?\n\nQty: ${preflight.computed_qty?.toFixed(6)}\nEntry: ${signal.entryPrice}\nSL: ${signal.stopLoss}\nTP: ${signal.tp2 || signal.tp1}\nRisk: $${preflight.risk_amount?.toFixed(2)}`;

                if (!window.confirm(confirmMsg)) {
                    setExecutingSignalId(null);
                    return;
                }

                // 3. Execute via manual API (uses same pipeline as bot)
                const result = await KrakenService.manualExecute(signal, apiConfig);

                if (result.success) {
                    alert(`? ORDER EXECUTED!\n\nOrder ID: ${result.order_id}\nSymbol: ${result.symbol}\nSide: ${result.side?.toUpperCase()}\nQty: ${result.qty}\nEntry: ${result.entry_price}`);

                    // Refresh positions
                    const pos = await KrakenService.getPositions(apiConfig);
                    setPositions(pos);
                } else {
                    alert(`? EXECUTION FAILED:\n${result.reason}`);
                }

            } catch (error: any) {
                console.error("Execution Error:", error);
                alert(`EXECUTION ERROR:\n${error.message}`);
            } finally {
                setExecutingSignalId(null);
            }

        } else {
            console.log("SIMULATING ORDER:", signal);
            alert(`SIMULATION: Executed ${signal.type} on ${signal.symbol}\n(Live mode disabled)`);
        }
    };

    // Check if a symbol has an open position
    const hasOpenPosition = (symbol: string): boolean => {
        const normalizedSymbol = symbol.replace(/[/:]/g, '').toUpperCase();
        return positions.some(p => {
            const posSymbol = (p.symbol || '').replace(/[/:]/g, '').toUpperCase();
            const posSize = parseFloat(p.total || '0');
            return posSymbol === normalizedSymbol && posSize > 0;
        });
    };

    return (
        <div className="min-h-screen bg-kraken-dark text-gray-300 font-sans selection:bg-kraken-cyan selection:text-black flex flex-col">
            {/* Header */}
            <header className="h-14 lg:h-16 border-b border-kraken-border bg-kraken-card flex items-center justify-between px-4 lg:px-6 sticky top-0 z-50 shrink-0">
                <div className="flex items-center gap-2 lg:gap-3">
                    <div className="w-7 h-7 lg:w-8 lg:h-8 bg-gradient-to-br from-kraken-cyan to-blue-600 rounded flex items-center justify-center font-bold text-black text-sm lg:text-base">
                        K
                    </div>
                    <h1 className="text-lg lg:text-xl font-bold tracking-tight text-white leading-none">
                        Kraken <span className="hidden sm:inline">SMC</span> <span className="text-kraken-cyan text-sm font-normal hidden sm:inline">Trader</span>
                    </h1>
                </div>

                <div className="flex items-center gap-3 lg:gap-6">
                    {/* Current Price Display - Compact on Mobile */}
                    <div className="flex flex-col items-end">
                        <span className="text-[10px] text-gray-500 hidden sm:block">Current Price</span>
                        <div className="flex items-center gap-1 text-white font-mono font-bold text-sm lg:text-lg">
                            <DollarSign size={14} className="text-kraken-cyan" />
                            {currentPrice.toFixed(2)}
                        </div>
                    </div>

                    <div className="h-8 w-px bg-kraken-border hidden lg:block"></div>

                    {/* Connection Status */}
                    <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-kraken-dark border border-kraken-border">
                        <div className={`w-2 h-2 rounded-full ${backendStatus === 'CONNECTED' ? 'bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.5)]' :
                            backendStatus === 'CONNECTING' ? 'bg-yellow-500 animate-pulse' :
                                'bg-red-500 shadow-[0_0_8px_rgba(239,68,68,0.5)]'
                            }`}></div>
                        <span className="text-[10px] font-bold uppercase tracking-tight text-gray-400">
                            {wrongBackend ? 'Wrong Backend!' :
                                backendStatus === 'CONNECTED' ? 'Backend Live' :
                                    backendStatus === 'CONNECTING' ? 'Connecting...' :
                                        'Backend Offline'}
                        </span>
                    </div>

                    {executionHealth && (
                        <div className={`hidden sm:flex items-center gap-1.5 px-2 py-1 rounded bg-kraken-dark border border-kraken-border cursor-help`} title={`Protocol: ${executionHealth.execution_mode}`}>
                            <Zap size={10} className={executionHealth.execution_mode === 'asymmetric' ? 'text-kraken-cyan' : 'text-gray-500'} />
                            <span className="text-[9px] font-bold uppercase tracking-tight text-gray-400">
                                {executionHealth.execution_mode === 'asymmetric' ? 'Maker-Taker' : 'Strict Mirror'}
                            </span>
                            {executionHealth.cooldown_active && (
                                <span className="ml-1 w-1.5 h-1.5 rounded-full bg-yellow-500 animate-pulse" title="Secondary Cooldown Active" />
                            )}
                        </div>
                    )}

                    <div className="h-8 w-px bg-kraken-border hidden lg:block"></div>

                    {/* Analysis Time - Desktop Only */}
                    {activeTab === 'DASHBOARD' && lastAnalysisTime && (
                        <div className="hidden lg:flex items-center gap-1 text-gray-500 bg-kraken-border/30 px-2 py-1 rounded text-xs">
                            <Clock size={12} />
                            <span>{lastAnalysisTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                        </div>
                    )}

                    {apiConfig.liveMode && (
                        <div className="flex items-center gap-1 text-kraken-red bg-kraken-red/10 px-2 py-0.5 lg:px-3 lg:py-1 rounded-full border border-kraken-red/20 animate-pulse">
                            <div className="w-1.5 h-1.5 lg:w-2 lg:h-2 rounded-full bg-kraken-red"></div>
                            <span className="text-[10px] lg:text-xs font-bold uppercase tracking-wider">Live</span>
                        </div>
                    )}

                    <div className="flex items-center gap-2">
                        {activeTab === 'DASHBOARD' ? (
                            <>
                                <button
                                    onClick={runAnalysis}
                                    disabled={loading}
                                    className="p-2 hover:bg-kraken-border rounded-full transition-colors disabled:opacity-50 text-gray-300"
                                    title="Refresh"
                                >
                                    <RefreshCw size={18} className={loading ? "animate-spin" : ""} />
                                </button>
                                <button
                                    onClick={() => setActiveTab('STATS')}
                                    className="p-2 hover:bg-kraken-border rounded-full transition-colors text-gray-300 hover:text-kraken-green"
                                    title="Trading Stats"
                                >
                                    <PieChart size={18} />
                                </button>
                                <button
                                    onClick={() => setActiveTab('GOVERNANCE')}
                                    className={`p-2 hover:bg-kraken-border rounded-full transition-colors ${activeTab === 'GOVERNANCE' ? 'text-purple-400' : 'text-gray-300 hover:text-purple-400'}`}
                                    title="System Governance"
                                >
                                    <BrainCircuit size={18} />
                                </button>
                                <button
                                    onClick={() => setActiveTab('ANALYTICS')}
                                    className={`p-2 hover:bg-kraken-border rounded-full transition-colors ${activeTab === 'ANALYTICS' ? 'text-blue-400' : 'text-gray-300 hover:text-blue-400'}`}
                                    title="Data Explorer"
                                >
                                    <Database size={18} />
                                </button>
                                <button
                                    onClick={() => setActiveTab('SETTINGS')}
                                    className="p-2 hover:bg-kraken-border rounded-full transition-colors text-gray-300 hover:text-kraken-cyan"
                                    title="Settings"
                                >
                                    <Settings size={18} />
                                </button>
                            </>
                        ) : (
                            <button
                                onClick={() => setActiveTab('DASHBOARD')}
                                className="flex items-center gap-2 px-3 py-1.5 hover:bg-kraken-border rounded-lg transition-colors text-sm"
                            >
                                <ArrowLeft size={16} /> <span className="hidden sm:inline">Dashboard</span>
                            </button>
                        )}
                    </div>
                </div>
            </header>

            {/* Main Content */}
            <main className="flex-grow p-4 lg:p-6 overflow-hidden flex flex-col">

                {activeTab === 'SETTINGS' ? (
                    <div className="flex justify-center pt-2 lg:pt-10 h-full overflow-y-auto">
                        <SettingsView config={apiConfig} onSave={handleSaveConfig} onCancel={() => setActiveTab('DASHBOARD')} />
                    </div>
                ) : activeTab === 'STATS' ? (
                    <div className="h-full overflow-y-auto -m-4 lg:-m-6">
                        <Dashboard />
                    </div>
                ) : activeTab === 'GOVERNANCE' ? (
                    <div className="h-full overflow-y-auto -m-4 lg:-m-6">
                        <SystemGovernance />
                    </div>
                ) : activeTab === 'ANALYTICS' ? (
                    <div className="h-full overflow-y-auto -m-4 lg:-m-6">
                        <DataExplorer />
                    </div>
                ) : (
                    <>
                        {/* Mobile: Horizontal Symbol Selector */}
                        <div className="lg:hidden mb-4 overflow-x-auto pb-2 scrollbar-hide flex gap-2">
                            {DEFAULT_CONFIG.symbols.map(sym => (
                                <button
                                    key={sym}
                                    onClick={() => setSelectedSymbol(sym)}
                                    className={`px-4 py-2 rounded-lg whitespace-nowrap text-sm font-medium border transition-colors ${selectedSymbol === sym
                                        ? 'bg-kraken-card border-kraken-cyan text-white shadow-[0_0_10px_rgba(0,240,255,0.2)]'
                                        : 'bg-kraken-card border-kraken-border text-gray-400'
                                        }`}
                                >
                                    {sym}
                                </button>
                            ))}
                        </div>

                        {/* Mobile: Tab Navigation */}
                        <div className="lg:hidden grid grid-cols-3 gap-1 bg-kraken-card p-1 rounded-lg border border-kraken-border mb-4">
                            <button
                                onClick={() => setMobileTab('CHART')}
                                className={`flex items-center justify-center gap-2 py-2 rounded text-xs font-bold transition-all ${mobileTab === 'CHART' ? 'bg-kraken-border text-white' : 'text-gray-500 hover:text-gray-300'}`}
                            >
                                <LineChart size={14} /> Chart
                            </button>
                            <button
                                onClick={() => setMobileTab('ANALYSIS')}
                                className={`flex items-center justify-center gap-2 py-2 rounded text-xs font-bold transition-all ${mobileTab === 'ANALYSIS' ? 'bg-kraken-border text-white' : 'text-gray-500 hover:text-gray-300'}`}
                            >
                                <FileText size={14} /> Analysis
                            </button>
                            <button
                                onClick={() => setMobileTab('SIGNALS')}
                                className={`flex items-center justify-center gap-2 py-2 rounded text-xs font-bold transition-all ${mobileTab === 'SIGNALS' ? 'bg-kraken-border text-white' : 'text-gray-500 hover:text-gray-300'}`}
                            >
                                <Zap size={14} /> Signals
                                {signals.length > 0 && <span className="w-2 h-2 bg-kraken-cyan rounded-full"></span>}
                            </button>
                        </div>

                        {/* Main Grid Content */}
                        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6 h-full min-h-0">

                            {/* Left Sidebar: Market Watch (Desktop Only) */}
                            <div className="hidden lg:flex lg:col-span-1 bg-kraken-card border border-kraken-border rounded-lg flex-col overflow-hidden max-h-[calc(100vh-8rem)]">
                                <div className="p-4 border-b border-kraken-border">
                                    <h3 className="font-semibold text-white flex items-center gap-2">
                                        <BarChart2 size={16} className="text-kraken-cyan" /> Market Watch
                                    </h3>
                                </div>
                                <div className="flex-1 overflow-y-auto custom-scrollbar">
                                    {DEFAULT_CONFIG.symbols.map(sym => (
                                        <button
                                            key={sym}
                                            onClick={() => setSelectedSymbol(sym)}
                                            className={`w-full p-4 flex justify-between items-center hover:bg-kraken-border transition-colors border-b border-kraken-border/50 ${selectedSymbol === sym ? 'bg-kraken-border/50 border-l-4 border-l-kraken-cyan' : 'border-l-4 border-l-transparent'}`}
                                        >
                                            <span className="font-medium text-white">{sym}</span>
                                            {selectedSymbol === sym && (
                                                <span className="text-xs text-gray-400 font-mono">{currentPrice > 0 ? currentPrice.toFixed(2) : '...'}</span>
                                            )}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Center Column: Chart & Analysis */}
                            <div className={`lg:col-span-2 flex flex-col gap-4 lg:gap-6 min-w-0 ${mobileTab !== 'CHART' && mobileTab !== 'ANALYSIS' ? 'hidden lg:flex' : 'flex'}`}>

                                {/* CHART VISUALIZER */}
                                <div className={`relative flex-grow min-h-[400px] lg:h-[500px] ${mobileTab === 'CHART' || 'lg:block' ? 'block' : 'hidden'}`}>
                                    {ltfAnalysis && <ChartVisualizer data={candles} analysis={ltfAnalysis} symbol={selectedSymbol} />}
                                    {loading && (
                                        <div className="absolute inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-10 rounded-lg">
                                            <div className="text-kraken-cyan animate-pulse">Analyzing Structure...</div>
                                        </div>
                                    )}
                                </div>

                                {/* ANALYSIS PANELS */}
                                <div className={`flex flex-col gap-4 ${mobileTab === 'ANALYSIS' || 'lg:block' ? 'block' : 'hidden lg:block'}`}>
                                    <div className="grid grid-cols-2 gap-4">
                                        <div className="bg-kraken-card border border-kraken-border p-4 rounded-lg">
                                            <h4 className="text-sm text-gray-400 mb-2">Structure (HTF)</h4>
                                            <div className="flex items-center gap-2">
                                                <div className={`text-xl lg:text-2xl font-bold ${htfAnalysis?.trend === Trend.BULLISH ? 'text-kraken-green' : htfAnalysis?.trend === Trend.BEARISH ? 'text-kraken-red' : 'text-gray-400'}`}>
                                                    {htfAnalysis?.trend || '...'}
                                                </div>
                                            </div>
                                            <div className="mt-2 text-xs text-gray-500">
                                                Pivots: {htfAnalysis?.pivots.length || 0}
                                            </div>
                                        </div>

                                        <div className="bg-kraken-card border border-kraken-border p-4 rounded-lg">
                                            <h4 className="text-sm text-gray-400 mb-2">Order Blocks (LTF)</h4>
                                            <div className="flex gap-4">
                                                <div className="text-center">
                                                    <span className="block text-xl lg:text-2xl font-bold text-kraken-green">
                                                        {ltfAnalysis?.orderBlocks.filter(o => o.type === 'BULLISH').length || 0}
                                                    </span>
                                                    <span className="text-xs text-gray-500">Bullish</span>
                                                </div>
                                                <div className="text-center">
                                                    <span className="block text-xl lg:text-2xl font-bold text-kraken-red">
                                                        {ltfAnalysis?.orderBlocks.filter(o => o.type === 'BEARISH').length || 0}
                                                    </span>
                                                    <span className="text-xs text-gray-500">Bearish</span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>

                                    {/* Open Positions Section */}
                                    {positions.length > 0 && (
                                        <div className="bg-kraken-card border border-kraken-border rounded-lg flex flex-col overflow-hidden">
                                            <div className="p-4 border-b border-kraken-border flex items-center justify-between">
                                                <h3 className="font-semibold text-white flex items-center gap-2">
                                                    <Wallet size={16} className="text-kraken-cyan" /> Positions
                                                </h3>
                                                <span className="text-xs text-gray-400">{positions.length} active</span>
                                            </div>
                                            <div className="overflow-x-auto">
                                                <table className="w-full text-left text-sm text-gray-400">
                                                    <thead className="bg-[#1B2129] uppercase text-xs font-semibold">
                                                        <tr>
                                                            <th className="px-4 py-3">Sym</th>
                                                            <th className="px-4 py-3">Side</th>
                                                            <th className="px-4 py-3 hidden sm:table-cell">Size</th>
                                                            <th className="px-4 py-3 hidden sm:table-cell">Entry</th>
                                                            <th className="px-4 py-3 text-right">PnL</th>
                                                        </tr>
                                                    </thead>
                                                    <tbody className="divide-y divide-kraken-border">
                                                        {positions.map((pos, i) => {
                                                            const isRecentSignal = signals.some(s => s.symbol === pos.symbol);
                                                            return (
                                                                <tr key={i} className={`hover:bg-kraken-border/30 transition-colors ${isRecentSignal ? 'bg-kraken-cyan/5 border-l-2 border-l-kraken-cyan' : 'border-l-2 border-l-transparent'}`}>
                                                                    <td className="px-4 py-3 font-medium text-white text-xs sm:text-sm">
                                                                        {pos.symbol.replace('USDT', '')}
                                                                        {isRecentSignal && <span className="ml-1 text-[8px] bg-kraken-cyan/20 text-kraken-cyan px-1 rounded">NEW</span>}
                                                                    </td>
                                                                    <td className="px-4 py-3">
                                                                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${pos.holdSide === 'long' ? 'bg-kraken-green/20 text-kraken-green' : 'bg-kraken-red/20 text-kraken-red'}`}>
                                                                            {pos.holdSide.toUpperCase()}
                                                                        </span>
                                                                    </td>
                                                                    <td className="px-4 py-3 hidden sm:table-cell">{pos.total}</td>
                                                                    <td className="px-4 py-3 hidden sm:table-cell">{parseFloat(pos.openPriceAvg).toFixed(4)}</td>
                                                                    <td className={`px-4 py-3 text-right font-medium text-xs sm:text-sm ${parseFloat(pos.unrealizedPL) >= 0 ? 'text-kraken-green' : 'text-kraken-red'}`}>
                                                                        {parseFloat(pos.unrealizedPL) > 0 ? '+' : ''}{parseFloat(pos.unrealizedPL).toFixed(2)}
                                                                    </td>
                                                                </tr>
                                                            )
                                                        })}
                                                    </tbody>
                                                </table>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* Right Column: Signals & Trade Feed */}
                            <div className={`lg:col-span-1 bg-kraken-card border border-kraken-border rounded-lg flex flex-col overflow-hidden lg:max-h-[calc(100vh-8rem)] ${mobileTab === 'SIGNALS' ? 'flex' : 'hidden lg:flex'}`}>
                                <div className="p-4 border-b border-kraken-border flex justify-between items-center">
                                    <h3 className="font-semibold text-white flex items-center gap-2">
                                        <Activity size={16} className="text-kraken-cyan" /> Signals
                                    </h3>
                                    {apiConfig.liveMode ? (
                                        <span className="text-xs font-bold text-kraken-red animate-pulse">LIVE MODE</span>
                                    ) : (
                                        <span className="text-xs text-gray-500">SIMULATION</span>
                                    )}
                                </div>

                                <div className="flex-1 overflow-y-auto p-4 space-y-3 custom-scrollbar">
                                    {/* Market Insight Card */}
                                    {htfAnalysis && (
                                        <div className={`p-4 rounded-md border-l-4 shadow-sm bg-[#151A1F] ${htfAnalysis.trend === Trend.BULLISH ? 'border-l-kraken-green' :
                                            htfAnalysis.trend === Trend.BEARISH ? 'border-l-kraken-red' : 'border-l-gray-500'
                                            }`}>
                                            <div className="flex items-center gap-2 mb-2">
                                                <BrainCircuit size={16} className="text-kraken-cyan" />
                                                <h4 className="text-sm font-bold text-white">Market Insight</h4>
                                            </div>
                                            <p className="text-xs text-gray-300 leading-relaxed font-mono">
                                                {analysisCommentary}
                                            </p>
                                        </div>
                                    )}

                                    {signals.length === 0 ? (
                                        <div className="text-center text-gray-500 py-6 flex flex-col items-center">
                                            <Zap size={24} className="mb-2 opacity-20" />
                                            <span className="text-xs">No active trade setups.</span>
                                        </div>
                                    ) : (
                                        signals.map((sig, idx) => (
                                            <div key={idx} className="bg-[#1B2129] border border-kraken-border p-3 rounded-md shadow-sm relative overflow-hidden group">
                                                <div className={`absolute left-0 top-0 bottom-0 w-1 ${sig.type === 'LONG' ? 'bg-kraken-green' : 'bg-kraken-red'}`}></div>
                                                <div className="flex justify-between items-start mb-2 pl-2">
                                                    <span className="font-bold text-white text-sm">{sig.symbol}</span>
                                                    <span className="text-[10px] text-gray-500">{new Date(sig.timestamp).toLocaleTimeString()}</span>
                                                </div>
                                                <div className="pl-2 space-y-1">
                                                    <div className={`text-xs font-bold ${sig.type === 'LONG' ? 'text-kraken-green' : 'text-kraken-red'}`}>
                                                        {sig.type} @ {sig.entryPrice}
                                                    </div>
                                                    <div className="grid grid-cols-2 gap-2 text-[10px] text-gray-400 mt-2">
                                                        <div>SL: <span className="text-gray-300">{sig.stopLoss}</span></div>
                                                        <div>TP: <span className="text-gray-300">{sig.tp2}</span></div>
                                                    </div>
                                                    <div className="mt-2 pt-2 border-t border-gray-800">
                                                        {sig.reason.map((r, i) => <div key={i} className="text-[10px] text-gray-500 flex items-center gap-1"><span className="w-1 h-1 rounded-full bg-gray-500"></span> {r}</div>)}
                                                    </div>
                                                </div>
                                                {/* Execute Button */}
                                                <button
                                                    onClick={() => handleExecuteSignal(sig)}
                                                    disabled={
                                                        executingSignalId === sig.timestamp ||
                                                        (apiConfig.liveMode && (!apiConfig.apiKey || !apiConfig.secretKey)) ||
                                                        (apiConfig.liveMode && hasOpenPosition(sig.symbol))
                                                    }
                                                    className={`w-full mt-3 text-xs py-1.5 rounded font-medium transition-colors flex items-center justify-center gap-2 ${hasOpenPosition(sig.symbol) && apiConfig.liveMode
                                                            ? 'bg-gray-700/50 text-gray-500 cursor-not-allowed border border-gray-600'
                                                            : apiConfig.liveMode
                                                                ? 'bg-kraken-red/20 text-kraken-red hover:bg-kraken-red hover:text-white border border-kraken-red/50'
                                                                : 'bg-kraken-border hover:bg-gray-700 text-gray-300'
                                                        }`}
                                                >
                                                    {executingSignalId === sig.timestamp ? (
                                                        <>
                                                            <Loader2 size={14} className="animate-spin" />
                                                            Executing...
                                                        </>
                                                    ) : hasOpenPosition(sig.symbol) && apiConfig.liveMode ? (
                                                        'Position Open'
                                                    ) : (
                                                        apiConfig.liveMode ? 'Execute Live Order' : 'Simulate Execution'
                                                    )}
                                                </button>
                                            </div>
                                        ))
                                    )}
                                </div>
                            </div>

                        </div>
                    </>
                )}
            </main>
        </div>
    );
};

export default App;
