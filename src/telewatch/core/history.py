import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

class HistoryManager:
    """Manages historical data of process runs."""
    
    def __init__(self, history_file: str = None):
        """Initialize history manager.
        
        Args:
            history_file: Path to the history JSON file.
        """
        if history_file is None:
            history_file = str(Path.home() / ".telewatch" / "history.json")
            
        self.history_file = Path(history_file)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        self.history: List[Dict[str, Any]] = self._load_history()
        
    def _load_history(self) -> List[Dict[str, Any]]:
        """Load history from file."""
        if not self.history_file.exists():
            return []
        try:
            with open(self.history_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading history: {e}")
            return []
            
    def _save_history(self) -> None:
        """Save history to file."""
        try:
            # Keep only last 100 runs
            if len(self.history) > 100:
                self.history = self.history[-100:]
            
            with open(self.history_file, 'w') as f:
                json.dump(self.history, f, indent=2)
        except Exception as e:
            print(f"Error saving history: {e}")

    def record_run(self, process_name: str, start_time: datetime, duration_seconds: float, status: str) -> None:
        """Record a completed run.
        
        Args:
            process_name: Name of the process.
            start_time: When it started.
            duration_seconds: How long it took.
            status: Final status (completed, failed, error).
        """
        run_data = {
            "process_name": process_name,
            "start_time": start_time.isoformat(),
            "duration_seconds": duration_seconds,
            "status": status,
            "timestamp": datetime.now().isoformat()
        }
        self.history.append(run_data)
        self._save_history()

    def get_average_duration(self, process_name: str) -> float:
        """Calculate average duration for a specific process.
        
        Args:
            process_name: Name of the process.
            
        Returns:
            Average duration in seconds, or 0 if no history.
        """
        relevant_runs = [
            run["duration_seconds"] for run in self.history 
            if run["process_name"] == process_name and run["status"] == "completed"
        ]
        if not relevant_runs:
            return 0.0
        return sum(relevant_runs) / len(relevant_runs)

    def get_recent_runs(self, limit: int = 5) -> List[Dict]:
        """Get recent runs from history."""
        return self.history[-limit:]
