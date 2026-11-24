import os
import sys
import logging
from pathlib import Path

# Important: load_dotenv can only be imported conditionally or we manually parse to enforce no-.env in PROD.
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

logger = logging.getLogger(__name__)

# Cache of loaded secrets to be used by the redactor
_LOADED_SECRETS = set()

class SecuritySettings:
    """Security specific constants and enforcement flags."""
    TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()
    
    @classmethod
    def enforce_prod_safety(cls):
        """Hard enforcement to prevent .env files from being used in production."""
        if cls.TRADING_MODE == "live":
            # Check current working directory and common config locations for .env
            env_paths = [
                Path.cwd() / ".env",
                Path(__file__).resolve().parent.parent.parent / ".env"
            ]
            for p in env_paths:
                if p.exists():
                    msg = (
                        f"CRITICAL OPSEC VIOLATION: A physical '{p.name}' file was found at {p}. "
                        "In 'live' TRADING_MODE, secrets MUST be injected via environment variables "
                        "(e.g., from AWS Secrets Manager or HashiCorp Vault). "
                        "Remove the file to start the system."
                    )
                    sys.stderr.write(f"{msg}\n")
                    sys.exit(1)


def init_secrets():
    """Initialize the environment based on mode."""
    SecuritySettings.enforce_prod_safety()
    
    if SecuritySettings.TRADING_MODE != "live" and load_dotenv:
        load_dotenv()
        logger.debug("Loaded secrets from local .env for development.")


def load_secret(var_name: str, required: bool = True, default: str = "") -> str:
    """
    Safely load a secret from the OS environment, register it for auto-redaction,
    and return it.
    
    Args:
        var_name: The name of the environment variable (e.g., EXEC_BITGET_API_KEY)
        required: If True, raises an error if the secret is missing.
        default: Fallback value if not required.
    """
    value = os.getenv(var_name, default)
    
    if required and not value:
        msg = f"CRITICAL: Missing required secret variable: {var_name}"
        logger.error(msg)
        raise EnvironmentError(msg)
        
    if value:
        # Register for redaction so it never leaks in the logs
        _LOADED_SECRETS.add(value)
        
    return value

def get_registered_secrets() -> set:
    """Returns the set of all loaded secrets to feed the redactor."""
    return _LOADED_SECRETS
