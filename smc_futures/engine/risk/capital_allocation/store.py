import json
import os
import logging
from typing import Dict, Any, Optional
from threading import Lock

logger = logging.getLogger(__name__)

class AllocationStateStore:
    """
    Thread-safe persistent store for allocation state per symbol.
    Uses a simple JSON file with atomic write (write to tmp -> rename).
    """
    def __init__(self, path: str = "smc_futures/data/allocation_state.json"):
        self.path = path
        self.lock = Lock()
        self._ensure_dir()
        self.state: Dict[str, Dict[str, Any]] = self._load()

    def _ensure_dir(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"[STORE] Failed to load state from {self.path}: {e}")
        return {}

    def get_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return self.state.get(symbol)

    def update_state(self, symbol: str, data: Dict[str, Any]):
        with self.lock:
            self.state[symbol] = data
            self._save()

    def _save(self):
        tmp_path = f"{self.path}.tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(self.state, f, indent=4)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.error(f"[STORE] Failed to save state to {self.path}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
