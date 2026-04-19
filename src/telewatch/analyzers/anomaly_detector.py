import time
import hashlib
import re
from typing import Dict, List, Optional, Set, Any
from collections import deque

class AnomalyDetector:
    """Detects temporal and structural anomalies in log streams."""
    
    def __init__(self, window_size_seconds: int = 60, spike_threshold: float = 3.0, 
                 stall_seconds: int = 300, config: Optional[Dict[str, Any]] = None):
        """Initialize anomaly detector.
        
        Args:
            window_size_seconds: Time window for frequency calculations
            spike_threshold: Multiplier for spike detection (default 3.0)
            stall_seconds: Time without logs to trigger stall alert (default 300)
            config: Optional config dict to override defaults
        """
        # If config provided, use those values
        if config:
            self.spike_threshold = config.get('spike_threshold', spike_threshold)
            self.stall_seconds = config.get('stall_seconds', stall_seconds)
            self.novelty_threshold = config.get('novelty_threshold', 0.8)
        else:
            self.spike_threshold = spike_threshold
            self.stall_seconds = stall_seconds
            self.novelty_threshold = 0.8
        
        self.window_size = window_size_seconds
        
        # Frequency tracking
        self.log_timestamps = deque()
        self.baseline_frequency: Optional[float] = None
        self.last_frequency: float = 0.0
        
        # Stall detection
        self.last_log_time = time.time()
        self.is_stalled = False
        
        # Novelty detection (Structural Fingerprinting)
        self.known_fingerprints: Set[str] = set()
        
    def add_event(self, line: str) -> Dict[str, Any]:
        """Record a log event and return detected anomalies."""
        now = time.time()
        self.last_log_time = now
        self.is_stalled = False
        
        # 1. Frequency Tracking
        self.log_timestamps.append(now)
        # Clear old timestamps
        while self.log_timestamps and self.log_timestamps[0] < now - self.window_size:
            self.log_timestamps.popleft()
            
        current_freq = len(self.log_timestamps) / (self.window_size / 60.0) # Lines per minute
        self.last_frequency = current_freq
        
        anomalies = []
        
        # 2. Spike Detection
        if self.baseline_frequency is None:
            if len(self.log_timestamps) > 10: # Wait for some data
                self.baseline_frequency = current_freq
        else:
            if current_freq > self.baseline_frequency * self.spike_threshold:
                anomalies.append({
                    "type": "spike",
                    "severity": "warning",
                    "message": f"Log frequency spike detected: {current_freq:.1f} L/min (Baseline: {self.baseline_frequency:.1f})"
                })
            # Slowly update baseline (moving average)
            self.baseline_frequency = self.baseline_frequency * 0.95 + current_freq * 0.05
            
        # 3. Novelty Detection
        fingerprint = self._get_fingerprint(line)
        is_novel = False
        if fingerprint not in self.known_fingerprints:
            is_novel = True
            self.known_fingerprints.add(fingerprint)
            # Limit size of fingerprints set
            if len(self.known_fingerprints) > 1000:
                self.known_fingerprints.pop() # Remove a random one is okay for simple set
                
        return {
            "anomalies": anomalies,
            "is_novel": is_novel,
            "current_frequency": current_freq
        }
        
    def check_stall(self) -> Optional[Dict[str, Any]]:
        """Check if the log stream has stalled.
        
        Returns:
            Anomaly dict if stalled, else None.
        """
        if self.is_stalled:
            return None
            
        if time.time() - self.last_log_time > self.stall_seconds:
            self.is_stalled = True
            return {
                "type": "stall",
                "severity": "critical",
                "message": f"Log stream stall detected! No logs for {self.stall_seconds} seconds."
            }
        return None

    def _get_fingerprint(self, line: str) -> str:
        """Generate a structural fingerprint of a log line.
        
        Replaces variable parts (dates, numbers, hex) with placeholders.
        """
        # 1. Clean numbers
        line = re.sub(r'\b\d+\b', '<NUM>', line)
        # 2. Clean hex/IDs
        line = re.sub(r'\b0x[a-fA-F0-9]+\b', '<HEX>', line)
        # 3. Clean common UUID patterns
        line = re.sub(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', '<UUID>', line)
        # 4. Clean dates/times (aggressive)
        line = re.sub(r'\d{2,4}[-/]\d{1,2}[-/]\d{1,2}', '<DATE>', line)
        line = re.sub(r'\d{1,2}:\d{2}:\d{2}', '<TIME>', line)
        
        return hashlib.md5(line.encode()).hexdigest()

    def get_stats(self) -> Dict[str, Any]:
        """Get summary statistics for anomaly detection."""
        return {
            "frequency": self.last_frequency,
            "baseline": self.baseline_frequency or 0.0,
            "known_structures": len(self.known_fingerprints)
        }
