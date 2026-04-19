"""Tests for Turbo Mode functionality."""

import unittest
import time
from unittest.mock import MagicMock
from telewatch.analyzers.event_analyzer import EventAnalyzer
from telewatch.analyzers.llm_client import BaseLLMClient
from telewatch.monitors.base import MonitorEvent, Severity
from telewatch.config import Config

class MockLLM(BaseLLMClient):
    def analyze(self, prompt): return 'test'

class TestTurboMode(unittest.TestCase):
    """Test suite for turbo mode optimizations."""
    
    def test_components_disabled_in_turbo(self):
        """Verify components are None in turbo mode."""
        analyzer = EventAnalyzer(MockLLM(), turbo=True)
        self.assertIsNone(analyzer.profiler)
        self.assertIsNone(analyzer.anomaly_detector)
    
    def test_components_enabled_in_normal(self):
        """Verify components exist in normal mode."""
        analyzer = EventAnalyzer(MockLLM(), turbo=False)
        self.assertIsNotNone(analyzer.profiler)
        self.assertIsNotNone(analyzer.anomaly_detector)
        
    def test_execution_safety_in_turbo(self):
        """Verify analyze_event runs without errors in turbo mode."""
        analyzer = EventAnalyzer(MockLLM(), turbo=True)
        event = MonitorEvent(
            source='test', 
            content='test log', 
            severity=Severity.INFO,
            timestamp=time.time(),
            metadata={}
        )
        
        try:
            analysis = analyzer.analyze_event(event)
            self.assertIsNotNone(analysis)
        except AttributeError as e:
            self.fail(f"Turbo mode execution failed with AttributeError: {e}")
            
    def test_monitor_manager_turbo_flag(self):
        """Verify MonitorManager passes turbo flag correctly."""
        from telewatch.cli import MonitorManager
        
        # Mock config
        mock_config = MagicMock(spec=Config)
        mock_config.get_llm_config.return_value = {'provider': 'openai', 'api_key': 'test'}
        mock_config.get_llm_optimization_config.return_value = {}
        mock_config.get_telegram_config.return_value = {'bot_token': 'test', 'chat_id': 'test'}
        mock_config.get_notification_config.return_value = {'rate_limit_per_hour': 10}
        
        # Mock create_llm_client to avoid import errors or API calls
        with unittest.mock.patch('telewatch.cli.create_llm_client') as mock_create:
            mock_create.return_value = MockLLM()
            
            # Test Turbo=True
            manager_turbo = MonitorManager(mock_config, turbo=True)
            self.assertTrue(manager_turbo.analyzer.turbo)
            self.assertIsNone(manager_turbo.analyzer.profiler)
            
            # Test Turbo=False
            manager_normal = MonitorManager(mock_config, turbo=False)
            self.assertFalse(manager_normal.analyzer.turbo)
            self.assertIsNotNone(manager_normal.analyzer.profiler)
