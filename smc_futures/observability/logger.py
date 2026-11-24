import logging
import json
import logging.config
import uuid
from typing import Dict, Any, Optional
from pythonjsonlogger import jsonlogger
from smc_futures.security.redaction import RedactingFormatter

# Extend JSON logger to support redaction of secrets within the JSON payload
class RedactingJsonFormatter(jsonlogger.JsonFormatter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Re-use our known secrets list from the Phase 0.75 security setup
        from smc_futures.security.secrets import _LOADED_SECRETS
        self.secrets = _LOADED_SECRETS

    def process_log_record(self, log_record):
        # Apply standard json processing
        log_record = super().process_log_record(log_record)
        
        # Redact any string values that contain a secret
        for key, value in log_record.items():
            if isinstance(value, str):
                for secret in self.secrets:
                    if secret and secret in value:
                        log_record[key] = value.replace(secret, "[REDACTED]")
        return log_record

class ContextAdapter(logging.LoggerAdapter):
    """
    Adapter that auto-injects correlation_id and other observability context
    into every log record.
    """
    def process(self, msg, kwargs):
        extra = kwargs.get('extra', {})
        # Merge adapter's context into the extra dict
        extra.update(self.extra)
        kwargs['extra'] = extra
        return msg, kwargs

def setup_json_logger(level: int = logging.INFO) -> None:
    """
    Sets up the root logger to output structured JSON.
    This replaces the legacy console format.
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    logHandler = logging.StreamHandler()
    # Ensure our standard set of fields is always present in the JSON root
    # such as timestamp, level, correlation_id, and message.
    formatter = RedactingJsonFormatter(
        '%(asctime)s %(levelname)s %(name)s %(correlation_id)s %(message)s'
    )
    logHandler.setFormatter(formatter)
    logger.addHandler(logHandler)

def get_context_logger(name: str, context: Optional[Dict[str, Any]] = None) -> ContextAdapter:
    """
    Gets a specialized logger tied to a specific execution context.
    Ideal for injecting `intent_id` into all logs generated for a single trade.
    """
    logger = logging.getLogger(name)
    if context is None:
        context = {}
        
    if "correlation_id" not in context:
        context["correlation_id"] = uuid.uuid4().hex[:8]
        
    return ContextAdapter(logger, context)
