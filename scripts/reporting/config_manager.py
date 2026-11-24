import re
import logging
import json
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class ConfigManager:
    """
    Safely manages modifications to smc_config.py.
    Uses literal replacement for robust updates.
    """
    
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.journal_dir = config_path.parent.parent / "reports" / "rollbacks"
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def _log_rollback(self, reason: str, updates: dict):
        """Write a rollback entry to a JSON journal."""
        journal_file = self.journal_dir / f"rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "updates": updates,
            "file_modified": str(self.config_path)
        }
        
        try:
            with open(journal_file, "w", encoding="utf-8") as f:
                json.dump(entry, f, indent=4)
            logger.info(f"[JOURNAL] Rollback recorded in {journal_file}")
        except Exception as e:
            logger.error(f"[JOURNAL] Failed to write rollback log: {e}")

    def get_last_rollback_time(self) -> float:
        """Returns the unix timestamp of the last rollback recorded in the journal."""
        last_time = 0
        try:
            entries = sorted(list(self.journal_dir.glob("rollback_*.json")))
            if entries:
                # Extract date from filename: rollback_YYYYMMDD_HHMMSS.json
                last_file = entries[-1].name
                date_str = last_file.replace("rollback_", "").replace(".json", "")
                dt = datetime.strptime(date_str, "%Y%m%d_%H%M%S")
                last_time = dt.replace(tzinfo=timezone.utc).timestamp()
        except:
            pass
        return last_time

    def update_constants(self, updates: dict, dry_run: bool = False) -> bool:
        """
        Update multiple constants in the config file.
        Example: updates = {"ASIA_MAX_RANGE_RATIO_BTC": 6.0}
        """
        if not self.config_path.exists():
            logger.error(f"Config file not found at {self.config_path}")
            return False

        if dry_run:
            logger.info(f"[DRY-RUN] Would update {len(updates)} constants in {self.config_path}")
            return True

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            modified = False
            new_lines = []
            
            for line in lines:
                updated_line = line
                for key, value in updates.items():
                    if f'"{key}"' in line:
                        colon_idx = line.find(":")
                        comma_idx = line.find(",", colon_idx)
                        if colon_idx != -1:
                            prefix = line[:colon_idx + 1]
                            suffix = line[comma_idx:] if comma_idx != -1 else ",\n"
                            updated_line = f"{prefix} {value}{suffix}"
                            modified = True
                            logger.info(f"[CONFIG] Updated {key} to {value}")
                            break
                new_lines.append(updated_line)

            if modified:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)
                return True
            
            return False

        except Exception as e:
            logger.error(f"Error updating config file: {e}")
            return False

    def rollback_asia_thresholds(self, reason: str = "PERFORMANCE_DROP", dry_run: bool = False) -> bool:
        """Rollback Asia thresholds to safe industrial baselines."""
        
        # ── Anti-Thrash Guard ──
        last_rollback = self.get_last_rollback_time()
        now = datetime.now(timezone.utc).timestamp()
        if (now - last_rollback) < 24 * 3600:
            logger.warning(f"[ROLLBACK] ABORTED - Anti-thrash guard active (last rollback < 24h ago)")
            return False

        baselines = {
            "ASIA_MAX_RANGE_RATIO_BTC": 6.0,
            "ASIA_MAX_RANGE_RATIO_ETH": 5.0,
            "ASIA_MAX_RANGE_RATIO_ALT": 4.0,
            "ASIA_RISK_FACTOR": 0.5
        }
        
        if dry_run:
            logger.info(f"[DRY-RUN] Rollback triggered by: {reason}")
            return self.update_constants(baselines, dry_run=True)

        logger.warning(f"[ROLLBACK] Triggering automated rollback of Asia thresholds. Reason: {reason}")
        success = self.update_constants(baselines)
        if success:
            self._log_rollback(reason, baselines)
        return success
