from abc import ABC, abstractmethod
from typing import Dict, Any
from .models import ScaleReadinessSnapshot, AllocationDecision

class AllocationPolicy(ABC):
    """
    Abstract base class for capital allocation policies.
    """
    @abstractmethod
    def decide(self, snapshot: ScaleReadinessSnapshot, current_capital: float) -> AllocationDecision:
        """
        Determine the next allocation decision based on the current state and metrics.
        """
        pass
