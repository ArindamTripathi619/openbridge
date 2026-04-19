"""Tests for the AnomalyDetector component."""

import unittest
import time
from telewatch.analyzers.anomaly_detector import AnomalyDetector

class TestAnomalyDetector(unittest.TestCase):
    """Test suite for anomaly detection functionality."""
    
    def test_spike_detection_basic(self):
        """Test basic spike detection when log frequency exceeds threshold."""
        # Use a very short window to make frequency calculation sensitive
        detector = AnomalyDetector(config={'spike_threshold': 2.0}, window_size_seconds=10)
        
        # Establish baseline
        # Add events separated by time to establish a low frequency
        # We need > 10 events to set baseline
        for i in range(15):
            detector.add_event(f"Normal log {i}")
            # Simulate time passing? No, add_event uses real time.
            # We can't easily simulate time unless we mock time.time 
            # OR we verify the logic that sets baseline.
        
        # Since we can't easily mock time inside the class without dependency injection or patching,
        # let's try to trigger it by adding many events rapidly.
        
        # Baseline is set after 10 events.
        # Current freq = lines / (window/60)
        
        # Let's manually set baseline to avoid timing issues
        detector.baseline_frequency = 10.0 # 10 lines per minute
        
        # Now add massive spike
        # Add 100 events
        result = None
        for i in range(20):
             result = detector.add_event(f"Spike log {i}")
        
        # Check if any result has anomalies
        spike_found = False
        if result and 'anomalies' in result:
            for anomaly in result['anomalies']:
                if anomaly['type'] == 'spike':
                    spike_found = True
                    break
                    
        self.assertTrue(spike_found, "Should detect spike when frequency exceeds threshold")
    
    def test_stall_detection(self):
        """Test stall detection after no logs for configured duration."""
        config = {'stall_seconds': 0.01}  # Very short timeout
        detector = AnomalyDetector(config=config)
        
        # Add initial log
        detector.add_event("Initial log")
        
        # Wait longer than stall threshold
        time.sleep(0.02)
        
        # Check stall
        stall_result = detector.check_stall()
        
        self.assertIsNotNone(stall_result, "Should return anomaly dict when stalled")
        self.assertEqual(stall_result['type'], 'stall')

    def test_novelty_detection_known_pattern(self):
        """Test that known patterns don't trigger novelty alerts."""
        detector = AnomalyDetector()
        
        # Establish known pattern
        pattern_line = "ERROR: Connection timeout at 192.168.1.100"
        detector.add_event(pattern_line)
        
        # Same structural pattern
        similar_line = "ERROR: Connection timeout at 192.168.1.101"
        result = detector.add_event(similar_line)
        
        # Check no novelty
        # Result dict: {'anomalies': [], 'is_novel': bool, ...}
        self.assertFalse(result['is_novel'], "Known pattern should not be novel")
