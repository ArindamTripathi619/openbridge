"""Automatic log profiling and structure discovery."""

import re
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Set
from datetime import datetime


@dataclass
class LogProfile:
    """Discovered structure of a log stream."""
    format_type: str = "unknown"  # json, syslog, csv, unstructured
    delimiter: Optional[str] = None
    has_timestamp: bool = False
    timestamp_pattern: Optional[str] = None
    common_patterns: List[str] = field(default_factory=list)
    field_count: int = 0
    sample_rate: float = 1.0


class LogProfiler:
    """Analyze log samples to discover structure and baselines."""
    
    def __init__(self, sample_limit: int = 100):
        self.sample_limit = sample_limit
        self.samples: List[str] = []
        self.is_profiled = False
        self.profile = LogProfile()
        self.drift_count = 0
        
    def reset(self):
        """Reset profiling state to start learning again."""
        self.samples = []
        self.is_profiled = False
        self.profile = LogProfile()
        self.drift_count = 0

    def check_drift(self, line: str) -> bool:
        """Check if line deviates significantly from current profile.
        
        Returns:
            True if structural drift is detected.
        """
        if not self.is_profiled:
            return False
            
        deviates = False
        
        # 1. Check JSON consistency
        if self.profile.format_type == "json":
            try:
                json.loads(line)
            except:
                deviates = True
        
        # 2. Check Delimiter consistency
        elif self.profile.delimiter and self.profile.delimiter not in line:
            deviates = True
            
        # 3. Check Timestamp consistency
        if self.profile.has_timestamp and self.profile.timestamp_pattern:
            if not re.search(self.profile.timestamp_pattern, line):
                deviates = True
                
        if deviates:
            self.drift_count += 1
        else:
            # Gradually reduce drift count if lines match again (cooling)
            self.drift_count = max(0, self.drift_count - 0.1)
            
        # Threshold: if ~20% of recent lines (based on sample_limit) fail, it's drift
        return self.drift_count > (self.sample_limit * 0.2)

    def add_sample(self, line: str):
        """Add a log line to the profiling set."""
        if not self.is_profiled:
            if len(self.samples) < self.sample_limit:
                self.samples.append(line.strip())
                
            if len(self.samples) >= self.sample_limit:
                self.profile_stream()
        else:
            # Check for drift after profiling is complete
            if self.check_drift(line):
                # Drift detected! Reset and re-profile
                # print(f"⚠️ Structural drift detected! Re-profiling...")
                self.reset()
                self.samples.append(line.strip())

    def profile_stream(self) -> LogProfile:
        """Analyze collected samples to build a profile."""
        if not self.samples:
            return self.profile

        # 1. Check for JSON
        if self._check_json():
            self.profile.format_type = "json"
        
        # 2. Detect Delimiter
        elif not self.profile.delimiter:
            self.profile.delimiter = self._detect_delimiter()
            if self.profile.delimiter:
                self.profile.format_type = "structured"

        # 3. Detect Timestamp
        self.profile.has_timestamp, self.profile.timestamp_pattern = self._detect_timestamp()

        # 4. Extract Common Fragments (Noise Baseline)
        self.profile.common_patterns = self._extract_common_fragments()

        self.is_profiled = True
        return self.profile

    def _check_json(self) -> bool:
        """Check if samples are primarily JSON."""
        json_count = 0
        for sample in self.samples[:10]:
            try:
                json.loads(sample)
                json_count += 1
            except:
                pass
        return json_count > 5

    def _detect_delimiter(self) -> Optional[str]:
        """Detect common field delimiters."""
        delimiters = [',', '|', '\t', ' - ', ' : ']
        counts = Counter()
        
        for sample in self.samples:
            for d in delimiters:
                if d in sample:
                    counts[d] += 1
                    
        if not counts:
            return None
            
        # Return most frequent if it appears in > 80% of samples
        top_d, count = counts.most_common(1)[0]
        if count > len(self.samples) * 0.8:
            return top_d
        return None

    def _detect_timestamp(self) -> (bool, Optional[str]):
        """Detect timestamp presence and pattern."""
        patterns = {
            "iso8601": r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}',
            "syslog": r'[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}',
            "bracket": r'\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]'
        }
        
        for name, pattern in patterns.items():
            matches = sum(1 for s in self.samples if re.search(pattern, s))
            if matches > len(self.samples) * 0.7:
                return True, pattern
                
        return False, None

    def _extract_common_fragments(self) -> List[str]:
        """Identify common repeating fragments for noise reduction."""
        # Simple implementation: top repeating words/phrases
        all_words = []
        for s in self.samples:
            # Strip timestamps first
            if self.profile.timestamp_pattern:
                s = re.sub(self.profile.timestamp_pattern, '', s)
            words = re.findall(r'\b[A-Za-z]{4,}\b', s)
            all_words.extend(words)
            
        counter = Counter(all_words)
        # Return words appearing in > 50% of lines
        return [word for word, count in counter.items() if count > len(self.samples) * 0.5]
