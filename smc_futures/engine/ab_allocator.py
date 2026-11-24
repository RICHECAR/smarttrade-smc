import hashlib
import logging

logger = logging.getLogger(__name__)

def stable_hash(text: str) -> int:
    """
    Generate a stable integer hash from a string using SHA256.
    """
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)

def select_engine(key: str, seed: int, weight: float) -> str:
    """
    Deterministically select between Engine A (Static) and Engine B (Adaptive)
    based on a key, seed, and weight (0.0 to 1.0).
    
    Logic:
        - bucket = (stable_hash(seed:key) % 10000) / 10000.0
        - return "B" if bucket < weight else "A"
    """
    if weight <= 0.0:
        return "A"
    if weight >= 1.0:
        return "B"
        
    combined_key = f"{seed}:{key}"
    h_val = stable_hash(combined_key)
    bucket = (h_val % 10000) / 10000.0
    
    selected = "B" if bucket < weight else "A"
    
    logger.debug(f"[AB_ALLOCATOR] key={key} seed={seed} weight={weight} bucket={bucket:.4f} selected={selected}")
    return selected
