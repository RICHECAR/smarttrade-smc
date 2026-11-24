import logging
import re
from smc_futures.security.secrets import get_registered_secrets

class RedactingFormatter(logging.Formatter):
    """
    A custom logging formatter that dynamically replaces any loaded secret
    found in the log message with a redaction string '[REDACTED]'.
    """
    def __init__(self, fmt=None, datefmt=None, style='%', validate=True):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, validate=validate)
        self.redaction_mark = "[REDACTED]"

    def format(self, record):
        # We process the record's original message, preventing modifications to the underlying object.
        original_msg = super().format(record)
        
        secrets = get_registered_secrets()
        if not secrets:
            return original_msg
            
        redacted_msg = original_msg
        
        # Sort secrets by length descending to redact larger strings first
        # Avoiding partial redaction issues if one secret is a substring of another
        sorted_secrets = sorted(secrets, key=len, reverse=True)
        
        for secret_val in sorted_secrets:
            # Redact all secrets regardless of length (Phase 4 hardening)
            if secret_val:
                # Use string replace directly for performance natively in Python
                redacted_msg = redacted_msg.replace(secret_val, self.redaction_mark)
                
        return redacted_msg

def setup_secure_logging(level=logging.INFO):
    """
    Applies the RedactingFormatter to the root logger.
    Call this at the very beginning of the application lifecycle.
    """
    root_logger = logging.getLogger()
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    
    # Use our custom redacting formatter
    formatter = RedactingFormatter(
        '%(asctime)s - [%(name)s] - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(formatter)
    
    root_logger.addHandler(console_handler)
    root_logger.setLevel(level)

