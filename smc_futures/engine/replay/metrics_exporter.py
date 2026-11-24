import csv
import logging
import time
import os
from typing import List, Dict, Any

logger = logging.getLogger("replay_metrics")

class CSVMetricsExporter:
    """
    Tracks and exports the results of the Async Event Replay stress tests.
    Dumps execution costs, latencies, and rollback statuses to a CSV file.
    """
    def __init__(self, output_path: str = "stress_test_results.csv"):
        self.output_path = output_path
        self.records: List[Dict[str, Any]] = []
        
    def add_result(self, intent_id: str, success: bool, error: str, 
                   total_latency_ms: float, is_rollback: bool, exchange_results: Dict[str, Any]):
        """Records a single end-to-end event resolution."""
        
        # Flatten the exchange results for CSV cols safely
        k_res = exchange_results.get("kraken", {})
        b_res = exchange_results.get("bitget", {})
        
        record = {
            "intent_id": intent_id,
            "success": success,
            "total_latency_ms": round(total_latency_ms, 2),
            "is_rollback": is_rollback,
            "error_reason": error,
            "kraken_success": k_res.get("success", False),
            "kraken_error": k_res.get("error_msg", ""),
            "bitget_success": b_res.get("success", False),
            "bitget_error": b_res.get("error_msg", "")
        }
        self.records.append(record)
        
    def export(self):
        """Write all stored records to the CSV file."""
        if not self.records:
            logger.warning("No metrics to export.")
            return
            
        file_exists = os.path.isfile(self.output_path)
        keys = self.records[0].keys()
        
        try:
            with open(self.output_path, 'w', newline='', encoding='utf-8') as output_file:
                dict_writer = csv.DictWriter(output_file, keys)
                dict_writer.writeheader()
                dict_writer.writerows(self.records)
            logger.info(f"✅ Exported {len(self.records)} event records to {self.output_path}")
        except Exception as e:
            logger.error(f"Failed to export CSV: {e}")
