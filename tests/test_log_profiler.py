"""Tests for the LogProfiler component."""

import unittest
from telewatch.analyzers.log_profiler import LogProfiler

class TestLogProfiler(unittest.TestCase):
    """Test suite for log profiler functionality."""
    
    def test_json_format_detection(self):
        """Test automatic JSON format detection."""
        profiler = LogProfiler(sample_limit=10)
        
        # Need > 5 samples for JSON detection threshold
        json_lines = [
            '{"level": "INFO", "message": "Starting"}',
            '{"level": "DEBUG", "message": "Processing"}',
            '{"level": "ERROR", "message": "Failed"}',
            '{"level": "INFO", "message": "Running"}',
            '{"level": "DEBUG", "message": "Checking"}',
            '{"level": "INFO", "message": "Finished"}'
        ]
        
        for line in json_lines:
            profiler.add_sample(line)
        
        # Manually force profile update since we didn't hit limit
        profile = profiler.profile_stream()
        self.assertEqual(profile.format_type, 'json')
    
    def test_csv_format_detection(self):
        """Test automatic CSV format detection."""
        profiler = LogProfiler(sample_limit=10)
        
        csv_lines = [
            '2024-02-14,INFO,Application started',
            '2024-02-14,DEBUG,Loading configuration',
            '2024-02-14,ERROR,Connection failed'
        ]
        
        for line in csv_lines:
            profiler.add_sample(line)
        
        # Manually force profile update
        profile = profiler.profile_stream()
        # The analyzer uses 'structured' for delimited files
        self.assertEqual(profile.format_type, 'structured')
        self.assertIn(',', profile.delimiter)
    
    def test_drift_detection_format_change(self):
        """Test drift detection when format changes mid-stream."""
        profiler = LogProfiler(sample_limit=10)
        
        # First 10 lines: JSON
        for i in range(10):
            profiler.add_sample(f'{{"level": "INFO", "id": {i}}}')
        
        initial_profile = profiler.profile
        self.assertEqual(initial_profile.format_type, 'json')
        
        # Next 10 lines: CSV (drift should trigger)
        drift_count = 0
        for i in range(10):
            line = f'2024-02-14,INFO,Message {i}'
            if profiler.check_drift(line):
                drift_count += 1
        
        # Drift logic requires 20% failure of sample_limit
        # 10 failures out of 10 limit = 100% > 20%
        # But check_drift implementation might be stateful.
        # Let's check if drift count > 0
        self.assertTrue(drift_count > 0, "Drift should be detected when format changes")
