import logging
import time
from smc_futures.observability.logger import get_context_logger
from smc_futures.observability.metrics import WS_DESYNC_COUNT

alerts_logger = logging.getLogger("alerts")

def trigger_critical_alert(alert_type: str, message: str, context: dict = None):
    """
    Exécute les hook externes pour alerter l'opérateur sur Slack, PagerDuty ou Discord.
    Dans Phase 2, on simule la notification externe via un gros Log Error JSON formaté.
    """
    if context is None:
        context = {}
        
    log = get_context_logger("pager_duty_mock", context)
    
    # Track known Alert Types
    if alert_type == "WS_DESYNC":
        WS_DESYNC_COUNT.inc()
        
    # Output to stdout with CRITICAL severity.
    # In a real environment, this might post a payload to a Webhook URL.
    log.critical(
        f"🚨 [INSTITUTIONAL ALERT] {alert_type}: {message}",
        extra={"alert_type": alert_type, "timestamp_ms": int(time.time() * 1000)}
    )

def escalate_stale_drop_rate(rate: float):
    if rate > 0.05:  # e.g., > 5% logic drop
        trigger_critical_alert("STALE_TICK_DROP_RATE", f"{rate * 100}% of network executions failed staling test!")

def notify_partial_execution(intent_id: str):
    trigger_critical_alert(
        "PARTIAL_EXECUTION",
        "A partial execution fallback was triggered inside the STRICT OMS layer.",
        context={"correlation_id": intent_id}
    )
    
def notify_double_fill():
    trigger_critical_alert(
        "DOUBLE_FILL_DETECTED",
        "A race condition caused a double-fill DURING an active Rollback!"
    )
