
import unittest
from unittest.mock import MagicMock
import sys
import os
from datetime import datetime

# Ensure src is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.telewatch.analyzers.event_analyzer import EventAnalyzer, Analysis
from src.telewatch.monitors.base import MonitorEvent, Severity
from src.telewatch.analyzers.token_tracker import TokenUsageTracker

class TestEventAnalyzer(unittest.TestCase):
    def test_event_analyzer_caching(self):
        # Mock LLM Client
        llm_client = MagicMock()
        llm_client.analyze.return_value = '{"severity": "WARNING", "summary": "Test", "root_cause": "Test", "suggested_action": "Test"}'
        
        # Shared tracker
        tracker = TokenUsageTracker()
        
        analyzer = EventAnalyzer(
            llm_client=llm_client,
            token_tracker=tracker,
            optimization_config={'enable_cache': True}
        )
        
        # Ensure cache is fresh
        if analyzer.cache:
            analyzer.cache.clear()
        
        # Use standard timestamp format at the start
        content1 = "[2026-02-13 12:00:00] ERROR: Connection failed"
        event = MonitorEvent(
            timestamp=datetime.now(),
            source="test",
            severity=Severity.WARNING,
            content=content1,
            metadata={}
        )
        
        # 1. First analysis (Cache Miss)
        res1 = analyzer.analyze_event(event)
        self.assertEqual(llm_client.analyze.call_count, 1)
        self.assertEqual(tracker.get_stats()['llm_calls'], 1)
        self.assertEqual(tracker.get_stats()['cached_calls'], 0)
        
        # 2. Same event type, different time
        content2 = "[2026-02-13 12:01:00] ERROR: Connection failed"
        event2 = MonitorEvent(
            timestamp=datetime.now(),
            source="test",
            severity=Severity.WARNING,
            content=content2,
            metadata={}
        )
        res2 = analyzer.analyze_event(event2)
        
        # Should NOT call LLM again because signature (stripped content) is identical
        self.assertEqual(llm_client.analyze.call_count, 1)
        self.assertEqual(tracker.get_stats()['llm_calls'], 1)
        self.assertEqual(tracker.get_stats()['cached_calls'], 1)
        self.assertEqual(res1.summary, res2.summary)

    def test_event_analyzer_pattern_matching(self):
        llm_client = MagicMock()
        tracker = TokenUsageTracker()
        
        # SeverityPatternMatcher expects Dict[str, List[str]]
        patterns = {
            "info": [r"Connection pool stable", r"Heartbeat received"],
            "warning": [r"Disk usage high"],
            "critical": [r"Database connection lost"]
        }
        
        from src.telewatch.analyzers.pattern_matcher import SeverityPatternMatcher
        matcher = SeverityPatternMatcher(patterns)
        
        analyzer = EventAnalyzer(
            llm_client=llm_client,
            token_tracker=tracker,
            optimization_config={'use_local_patterns': True}
        )
        # Manually inject the matcher with our test patterns
        analyzer.pattern_matcher = matcher
        
        # This should match a pattern WITHOUT LLM
        event = MonitorEvent(
            timestamp=datetime.now(),
            source="test",
            severity=Severity.INFO,
            content="[12:00:00] INFO: Connection pool stable",
            metadata={}
        )
        res = analyzer.analyze_event(event)
        
        # LLM should not be called
        self.assertEqual(llm_client.analyze.call_count, 0)
        self.assertIn("Pattern-matched", res.summary)
        self.assertIn("Connection pool stable", res.summary)
        self.assertEqual(tracker.get_stats()['cached_calls'], 0)

if __name__ == "__main__":
    unittest.main()
