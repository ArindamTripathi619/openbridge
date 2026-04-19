"""Pattern-based severity detection to reduce LLM usage."""

import re
from typing import List, Dict, Optional
from ..monitors.base import Severity


class SeverityPatternMatcher:
    """Match severity levels using regex patterns."""
    
    def __init__(self, patterns: Dict[str, List[str]]):
        """Initialize pattern matcher.
        
        Args:
            patterns: Dictionary mapping severity names to pattern lists.
        """
        self.patterns = patterns
        self.dynamic_patterns: Dict[str, List[str]] = {
            "critical": [], "warning": [], "info": []
        }
        self.dynamic_compiled: Dict[str, List[re.Pattern]] = {
            "critical": [], "warning": [], "info": []
        }
        self._compile_patterns()
    
    def _compile_patterns(self) -> None:
        """Compile regex patterns for efficiency."""
        self.compiled = {}
        
        for severity_name, pattern_list in self.patterns.items():
            compiled_list = []
            for pattern in pattern_list:
                try:
                    compiled_list.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    print(f"Warning: Invalid regex pattern '{pattern}': {e}")
            
            self.compiled[severity_name] = compiled_list

    def add_dynamic_pattern(self, pattern: str, severity: Severity):
        """Add a pattern generated at runtime (e.g., by LLM)."""
        severity_name = severity.value
        if severity_name not in self.dynamic_patterns:
            return
            
        if pattern not in self.dynamic_patterns[severity_name]:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
                self.dynamic_patterns[severity_name].append(pattern)
                self.dynamic_compiled[severity_name].append(compiled)
                # print(f"Added dynamic pattern for {severity_name}: {pattern}")
            except re.error as e:
                print(f"Error adding dynamic pattern '{pattern}': {e}")
    
    def match(self, text: str) -> Optional[Severity]:
        """Match text against patterns to determine severity.
        
        Args:
            text: Text to match against patterns.
            
        Returns:
            Severity if match found, None otherwise.
        """
        # Check critical patterns first
        if self._matches_any(text, "critical"):
            return Severity.CRITICAL
        
        # Then warnings
        if self._matches_any(text, "warning"):
            return Severity.WARNING
        
        # Then info
        if self._matches_any(text, "info"):
            return Severity.INFO
        
        return None
    
    def _matches_any(self, text: str, severity_name: str) -> bool:
        """Check if text matches any pattern for severity.
        
        Args:
            text: Text to check.
            severity_name: Severity level name.
            
        Returns:
            True if any pattern matches.
        """
        # Check dynamic (learned) patterns first
        dynamic = self.dynamic_compiled.get(severity_name, [])
        if any(pattern.search(text) for pattern in dynamic):
            return True
            
        # Then fixed config patterns
        patterns = self.compiled.get(severity_name, [])
        return any(pattern.search(text) for pattern in patterns)
    
    def get_matching_pattern(self, text: str, severity_name: str) -> Optional[str]:
        """Get the first matching pattern for debugging.
        
        Args:
            text: Text to check.
            severity_name: Severity level name.
            
        Returns:
            Matching pattern string or None.
        """
        patterns = self.compiled.get(severity_name, [])
        for pattern in patterns:
            if pattern.search(text):
                return pattern.pattern
        return None


def get_default_patterns() -> Dict[str, List[str]]:
    """Get default severity patterns.
    
    Returns:
        Default pattern dictionary.
    """
    return {
        "critical": [
            r"segmentation fault|segfault",
            r"out of memory|oom|memory exhausted",
            r"panic|kernel panic",
            r"fatal\s+error",
            r"database\s+(connection\s+)?failed",
            r"cannot\s+connect\s+to\s+database",
            r"core dumped",
            r"stack overflow",
            r"deadlock detected",
            r"system\s+crash",
            r"unrecoverable\s+error",
        ],
        "warning": [
            r"deprecated",
            r"retry|retrying",
            r"timeout|timed\s+out",
            r"connection\s+(lost|dropped|closed)",
            r"warn(ing)?:",
            r"potential\s+issue",
            r"performance\s+degradation",
            r"disk\s+space\s+low",
            r"rate\s+limit",
            r"quota\s+exceeded",
        ],
        "info": [
            r"start(ed|ing)",
            r"complet(ed|ion)",
            r"initializ(ed|ing)",
            r"success(ful|fully)?",
            r"ready",
            r"listening\s+on",
            r"connected\s+to",
            r"shutdown",
        ]
    }
