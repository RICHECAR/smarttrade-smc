import time
import logging
import json
import os
from collections import deque
from logging.handlers import TimedRotatingFileHandler
from typing import Dict, Any, Optional
from smc_futures.observability.metrics import LATENCY_HISTOGRAM

logger = logging.getLogger(__name__)

class LatencyTracker:
    """
    Tracks micro-latencies across the execution pipeline with JSONL persistence.
    """
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(LatencyTracker, cls).__new__(cls)
        return cls._instance

    def __init__(self, max_history: int = 1000, log_path: str = "smc_futures/data/logs/latency_traces.jsonl"):
        if getattr(self, "_initialized", False):
            return
        self.history = deque(maxlen=max_history)
        # In-flight order timestamps: {order_id: {event: ts}}
        self.pending: Dict[str, Dict[str, Any]] = {}
        
        # Persistence setup
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self.jsonl_logger = logging.getLogger("latency_traces")
        self.jsonl_logger.setLevel(logging.INFO)
        # Rotate daily, keep 7 days
        handler = TimedRotatingFileHandler(log_path, when="D", interval=1, backupCount=7)
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.jsonl_logger.addHandler(handler)
        self._initialized = True

    def record_timestamp(self, order_id: str, event: str, timestamp: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None):
        """
        Record a timestamp for a specific event.
        Events:
            t_signal: Decision made by strategy
            t_order_send: Right before calling exchange REST API
            t_exchange_ack: When REST API returns (order_id received)
            t_ws_update: First WS event for this order
            t_fill: Final or partial fill event
        """
        if not order_id:
            return
            
        ts = timestamp or time.time()
        if order_id not in self.pending:
            self.pending[order_id] = {"metadata": metadata or {}}
        
        self.pending[order_id][event] = ts
        
        # If we have enough for a segment, record it
        self._check_and_emit_metrics(order_id)

    def _check_and_emit_metrics(self, order_id: str):
        p = self.pending.get(order_id)
        if not p:
            return

        # 1. Decision to Send
        if "t_signal" in p and "t_order_send" in p and "decision_to_send" not in p:
            val = (p["t_order_send"] - p["t_signal"]) * 1000
            LATENCY_HISTOGRAM.labels(type="decision_to_send").observe(val)
            p["decision_to_send"] = val

        # 2. REST RTT
        if "t_order_send" in p and "t_exchange_ack" in p and "rest_rtt" not in p:
            val = (p["t_exchange_ack"] - p["t_order_send"]) * 1000
            LATENCY_HISTOGRAM.labels(type="rest_rtt").observe(val)
            p["rest_rtt"] = val

        # 3. ACK to WS
        if "t_exchange_ack" in p and ("t_ws_update" in p or "t_ws_order_update_first" in p) and "ack_to_ws" not in p:
            ws_ts = p.get("t_ws_update") or p.get("t_ws_order_update_first")
            val = (ws_ts - p["t_exchange_ack"]) * 1000
            LATENCY_HISTOGRAM.labels(type="ack_to_ws").observe(val)
            p["ack_to_ws"] = val

        # 4. End to End (Signal to WS)
        if "t_signal" in p and ("t_ws_update" in p or "t_ws_order_update_first" in p) and "end_to_end" not in p:
            ws_ts = p.get("t_ws_update") or p.get("t_ws_order_update_first")
            val = (ws_ts - p["t_signal"]) * 1000
            LATENCY_HISTOGRAM.labels(type="end_to_end").observe(val)
            p["end_to_end"] = val

        # 5. Fill Latency
        if "t_signal" in p and "t_fill" in p and "fill_latency" not in p:
            val = (p["t_fill"] - p["t_signal"]) * 1000
            LATENCY_HISTOGRAM.labels(type="fill_latency").observe(val)
            p["fill_latency"] = val
            
            # Persist and clean up
            trace = {**p, "order_id": order_id, "ts_recorded": time.time()}
            self.history.append(trace)
            self.jsonl_logger.info(json.dumps(trace))
            
            # Clean up pending
            del self.pending[order_id]

    def get_stats(self) -> Dict[str, Any]:
        """Return aggregate stats for recent history."""
        if not self.history:
              return {}
            
        e2e = [h["end_to_end"] for h in self.history if "end_to_end" in h]
        if not e2e:
              return {}
            
        import numpy as np
        return {
              "p50_e2e": float(np.percentile(e2e, 50)),
              "p90_e2e": float(np.percentile(e2e, 90)),
              "p95_e2e": float(np.percentile(e2e, 95)),
              "avg_rest_rtt": float(np.mean([h["rest_rtt"] for h in self.history if "rest_rtt" in h])),
              "sample_count": len(self.history)
        }

# Global singleton
latency_tracker = LatencyTracker()

