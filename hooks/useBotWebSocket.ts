import { useEffect, useRef, useState, useCallback } from 'react';

interface BotSignal {
    type: string;
    symbol: string;
    signal?: any;
    state?: any;
    timestamp?: string;
}

/**
 * Hook that connects to the Python backend WebSocket
 * and receives real-time analysis updates & signals
 * WITHOUT polling the exchange API.
 */
export function useBotWebSocket() {
    const [connected, setConnected] = useState(false);
    const [latestSignal, setLatestSignal] = useState<any>(null);
    const [botState, setBotState] = useState<Record<string, any>>({});
    const wsRef = useRef<WebSocket | null>(null);
    const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

    const connect = useCallback(() => {
        // Get WS URL from env or fallback
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const envWsUrl = (import.meta as any).env?.VITE_WS_URL;

        let wsUrl: string;
        if (envWsUrl) {
            // Strip trailing /ws to prevent /ws/ws/trades
            const baseUrl = envWsUrl.replace(/\/ws\/?$/, '');
            wsUrl = `${baseUrl}/ws/trades`;
        } else {
            // Default: use current host with /ws/trades
            wsUrl = `${wsProtocol}//${window.location.host}/ws/trades`;
        }

        console.log('🔌 Connecting to WebSocket:', wsUrl);

        try {
            const ws = new WebSocket(wsUrl);
            wsRef.current = ws;

            ws.onopen = () => {
                console.log('🔌 Bot WebSocket connected');
                setConnected(true);
            };

            ws.onmessage = (event) => {
                try {
                    const data: BotSignal = JSON.parse(event.data);

                    if (data.type === 'signal' && data.signal) {
                        console.log(`📡 Signal received: ${data.symbol} ${data.signal.direction}`);
                        setLatestSignal({
                            ...data.signal,
                            symbol: data.symbol,
                            receivedAt: new Date().toISOString()
                        });
                    }

                    if (data.type === 'update' && data.state) {
                        setBotState(prev => ({
                            ...prev,
                            [data.symbol]: {
                                ...data.state,
                                updatedAt: new Date().toISOString()
                            }
                        }));
                    }
                } catch (e) {
                    // ignore parse errors
                }
            };

            ws.onclose = () => {
                console.log('🔌 Bot WebSocket disconnected, reconnecting in 5s...');
                setConnected(false);
                reconnectTimer.current = setTimeout(connect, 5000);
            };

            ws.onerror = () => {
                ws.close();
            };
        } catch (e) {
            console.error('WebSocket connection error:', e);
            reconnectTimer.current = setTimeout(connect, 5000);
        }
    }, []);

    useEffect(() => {
        connect();
        return () => {
            if (wsRef.current) wsRef.current.close();
            if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
        };
    }, [connect]);

    return { connected, latestSignal, botState };
}
