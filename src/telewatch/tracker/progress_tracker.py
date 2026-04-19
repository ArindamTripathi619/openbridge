"""Progress tracking and estimation."""

import re
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class ProgressSnapshot:
    """Represents a point-in-time progress snapshot."""
    timestamp: datetime
    percentage: float
    log_snippet: str
    source: str  # 'log-based', 'time-based', 'llm-assisted'


class ProgressTracker:
    """Tracks process completion progress and detects milestones."""
    
    def __init__(self, config: Dict):
        """Initialize progress tracker.
        
        Args:
            config: Process tracking configuration.
        """
        self.config = config
        self.process_name = config.get("name", "Process")
        self.description = config.get("description", "")
        self.keywords = config.get("keywords", [])
        self.expected_duration = config.get("expected_duration_minutes")
        self.completion_indicators = config.get("completion_indicators", [])
        
        # Tracking state
        self.start_time = datetime.now()
        self.last_reported_percentage = 0.0
        self.current_percentage = 0.0
        self.snapshots: List[ProgressSnapshot] = []
        self.recent_logs: List[str] = []
        self.max_recent_logs = 100
        
        # Settings
        self.update_interval = config.get("update_interval_percent", 10)
        self.min_update_interval = config.get("min_update_interval_seconds", 300)
        self.last_update_time = datetime.now()
        
        # Multi-stage tracking
        self.stages: List[Dict] = config.get("stages", [])
        self.current_stage_idx = 0
        self.stage_weights = self._calculate_stage_weights()
    
    def _calculate_stage_weights(self) -> List[float]:
        """Calculate weighted contribution of each stage to total progress."""
        if not self.stages:
            return []
        
        # Use provided weights or assume equal if missing
        weights = [float(s.get("weight", 1.0)) for s in self.stages]
        total_weight = sum(weights)
        if total_weight > 0:
            return [w / total_weight for w in weights]
        return [1.0 / len(self.stages)] * len(self.stages)
    
    def add_log_line(self, line: str) -> None:
        """Add a log line for analysis.
        
        Args:
            line: Log line to add.
        """
        self.recent_logs.append(line)
        # Keep only recent logs
        if len(self.recent_logs) > self.max_recent_logs:
            self.recent_logs.pop(0)
    
    def estimate_progress(self) -> Optional[float]:
        """Estimate current progress percentage.
        
        Returns:
            Progress percentage (0-100) or None if cannot estimate.
        """
        # Try log-based estimation first
        log_based = self._estimate_from_logs()
        if log_based is not None:
            self._record_snapshot(log_based, "log-based")
            return log_based
        
        # Try time-based estimation
        if self.expected_duration:
            time_based = self._estimate_from_time()
            if time_based is not None:
                self._record_snapshot(time_based, "time-based")
                return time_based
        
        # Could use LLM-assisted here in future
        return None
    
    def _estimate_from_logs(self) -> Optional[float]:
        """Extract progress from logs using patterns.
        
        Returns:
            Progress percentage or None.
        """
        if not self.recent_logs:
            return None
        
        # Check custom completion indicators first
        for indicator in self.completion_indicators:
            pattern = indicator.get("pattern")
            ind_type = indicator.get("type")
            
            for line in reversed(self.recent_logs[-20:]):  # Check recent 20 lines
                match = re.search(pattern, line)
                if match:
                    if ind_type == "percentage":
                        # Pattern like "Progress: 45%"
                        return float(match.group(1))
                    elif ind_type == "fraction":
                        # Pattern like "Processed 450/1000"
                        current = float(match.group(1))
                        total = float(match.group(2))
                        return (current / total) * 100 if total > 0 else None
        
        # Try common patterns
        common_patterns = [
            (r"(\d+(?:\.\d+)?)%", "percentage"),
            (r"(\d+)\s*/\s*(\d+)", "fraction"),
            (r"progress:\s*(\d+(?:\.\d+)?)", "percentage"),
            (r"completed:\s*(\d+(?:\.\d+)?)%", "percentage"),
        ]
        
        # Check for stage transitions
        if self.stages and self.current_stage_idx < len(self.stages):
            next_stage_idx = self.current_stage_idx + 1
            if next_stage_idx < len(self.stages):
                next_stage_pattern = self.stages[next_stage_idx].get("start_pattern")
                if next_stage_pattern:
                    for line in reversed(self.recent_logs[-10:]):
                        if re.search(next_stage_pattern, line, re.IGNORECASE):
                            self.current_stage_idx = next_stage_idx
                            print(f"🚩 Transitioned to stage: {self.stages[self.current_stage_idx].get('name')}")
                            break

        for line in reversed(self.recent_logs[-20:]):
            line_lower = line.lower()
            
            # 1. Logic for stage-based weighted progress
            if self.stages:
                # Add stage-relative progress to baseline progress of previous stages
                base_percent = sum(self.stage_weights[:self.current_stage_idx]) * 100
                current_stage_weight = self.stage_weights[self.current_stage_idx]
                
                # Check for percentage match in current stage
                for pattern, ptype in common_patterns:
                    match = re.search(pattern, line_lower)
                    if match:
                        if ptype == "percentage":
                            stage_pct = float(match.group(1))
                            return base_percent + (stage_pct * current_stage_weight)
                        elif ptype == "fraction":
                            current = float(match.group(1))
                            total = float(match.group(2))
                            if total > 0:
                                stage_pct = (current / total) * 100
                                return base_percent + (stage_pct * current_stage_weight)
                
                # If no percentage found but in a stage, return just the base
                return base_percent

            # 2. Original simple logic (no stages)
            for pattern, ptype in common_patterns:
                match = re.search(pattern, line_lower)
                if match:
                    if ptype == "percentage":
                        percent = float(match.group(1))
                        if 0 <= percent <= 100:
                            return percent
                    elif ptype == "fraction":
                        current = float(match.group(1))
                        total = float(match.group(2))
                        if total > 0:
                            percent = (current / total) * 100
                            if 0 <= percent <= 100:
                                return percent
        
        return None
    
    def _estimate_from_time(self) -> Optional[float]:
        """Estimate progress based on elapsed time.
        
        Returns:
            Progress percentage or None.
        """
        if not self.expected_duration:
            return None
        
        elapsed = (datetime.now() - self.start_time).total_seconds() / 60  # minutes
        expected = self.expected_duration
        
        if expected <= 0:
            return None
        
        percentage = min((elapsed / expected) * 100, 99.9)  # Cap at 99.9% until confirmed done
        return percentage
    
    def _record_snapshot(self, percentage: float, source: str) -> None:
        """Record a progress snapshot.
        
        Args:
            percentage: Progress percentage.
            source: Estimation source.
        """
        self.current_percentage = percentage
        
        snapshot = ProgressSnapshot(
            timestamp=datetime.now(),
            percentage=percentage,
            log_snippet=self.recent_logs[-1] if self.recent_logs else "",
            source=source
        )
        self.snapshots.append(snapshot)
        
        # Keep last 100 snapshots
        if len(self.snapshots) > 100:
            self.snapshots.pop(0)
    
    def should_send_update(self) -> bool:
        """Check if we should send a progress update.
        
        Returns:
            True if an update should be sent.
        """
        current = self.current_percentage
        last_reported = self.last_reported_percentage
        
        # Check if we've crossed a milestone
        milestone_crossed = int(current / self.update_interval) > int(last_reported / self.update_interval)
        
        # Check minimum time interval
        time_since_last = (datetime.now() - self.last_update_time).total_seconds()
        min_interval_met = time_since_last >= self.min_update_interval
        
        return milestone_crossed and min_interval_met
    
    def mark_update_sent(self) -> None:
        """Mark that an update was sent."""
        self.last_reported_percentage = self.current_percentage
        self.last_update_time = datetime.now()
    
    def get_elapsed_time(self) -> str:
        """Get formatted elapsed time.
        
        Returns:
            Formatted elapsed time string.
        """
        elapsed = datetime.now() - self.start_time
        hours = int(elapsed.total_seconds() // 3600)
        minutes = int((elapsed.total_seconds() % 3600) // 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    
    def get_estimated_remaining(self) -> Optional[str]:
        """Get estimated remaining time.
        
        Returns:
            Formatted remaining time or None.
        """
        if self.current_percentage <= 0 or self.current_percentage >= 100:
            return None
        
        elapsed = (datetime.now() - self.start_time).total_seconds()
        
        # Estimate based on current progress
        if self.current_percentage > 0:
            total_estimated = (elapsed / self.current_percentage) * 100
            remaining = total_estimated - elapsed
            
            if remaining > 0:
                hours = int(remaining // 3600)
                minutes = int((remaining % 3600) // 60)
                
                if hours > 0:
                    return f"{hours}h {minutes}m"
                else:
                    return f"{minutes}m"
        
        return None
    
    def is_stalled(self, threshold_minutes: int = 30) -> bool:
        """Check if progress appears stalled.
        
        Args:
            threshold_minutes: Minutes without progress to consider stalled.
            
        Returns:
            True if stalled.
        """
        if len(self.snapshots) < 2:
            return False
        
        # Check if percentage hasn't changed in threshold_minutes
        current_time = datetime.now()
        threshold_time = current_time - timedelta(minutes=threshold_minutes)
        
        recent_snapshots = [s for s in self.snapshots if s.timestamp > threshold_time]
        
        if len(recent_snapshots) < 2:
            return False
        
        # All recent snapshots have same percentage
        percentages = [s.percentage for s in recent_snapshots]
        return len(set(percentages)) == 1 and (current_time - self.snapshots[-1].timestamp).total_seconds() > threshold_minutes * 60
    
    def get_progress_bar(self, width: int = 10) -> str:
        """Generate a visual progress bar.
        
        Args:
            width: Width of the progress bar.
            
        Returns:
            Progress bar string with emojis.
        """
        filled = int((self.current_percentage / 100) * width)
        empty = width - filled
        
        return "⬛" * filled + "⬜" * empty
    
    def get_recent_activity(self, max_lines: int = 5) -> List[str]:
        """Get recent activity summary.
        
        Args:
            max_lines: Maximum number of activity lines.
            
        Returns:
            List of activity strings.
        """
        activity = []
        
        # Add recent log lines containing keywords
        for line in reversed(self.recent_logs[-20:]):
            if len(activity) >= max_lines:
                break
            
            # Check if line contains any keyword
            line_lower = line.lower()
            if any(kw.lower() in line_lower for kw in self.keywords):
                # Clean and format
                clean_line = line.strip()
                if len(clean_line) > 100:
                    clean_line = clean_line[:97] + "..."
                activity.append(f"• {clean_line}")
        
        return activity if activity else ["• No recent keyword matches in logs"]
