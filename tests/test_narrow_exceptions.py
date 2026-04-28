"""Tests for narrowed exception handling in critical paths."""
from __future__ import annotations

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from openbridge.opencode_bridge import OpenCodeBridge, BridgeConfig


def bridge_config():
    return BridgeConfig.from_mapping({"TELEGRAM_BOT_TOKEN": "x", "OPENCODE_WORKING_DIR": "."})


def test_opencode_request_json_decode_handled():
    """Verify json.JSONDecodeError is handled in _opencode_request_sync."""
    cfg = bridge_config()
    bridge = OpenCodeBridge(cfg)

    # Mock urlopen to return invalid JSON
    with patch("openbridge.opencode_bridge.urlopen") as mock_open:
        mock_response = MagicMock()
        mock_response.read.return_value = b"not valid json at all"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_open.return_value = mock_response

        result = bridge._opencode_request_sync("GET", "/test")
        assert isinstance(result, dict)
        assert "text" in result
        assert result["text"] == "not valid json at all"


def test_parse_decorator_json_handles_decode_error():
    """Verify json.JSONDecodeError is caught in _parse_decorator_json."""
    cfg = bridge_config()
    bridge = OpenCodeBridge(cfg)

    # Invalid JSON text should be handled gracefully
    result = bridge._parse_decorator_json("{ invalid json }")
    assert result is None


def test_extract_json_object_text_handles_type_error():
    """Verify TypeError is caught in _extract_json_object_text."""
    # Test with a dict input (wrong type) to trigger TypeError
    result = OpenCodeBridge._extract_json_object_text({"invalid": "type"})
    assert result is None


def test_call_chat_completion_narrows_exceptions():
    """Verify _call_chat_completion catches specific HTTP/network exceptions."""
    cfg = bridge_config()
    bridge = OpenCodeBridge(cfg)

    runtime = {
        "model": "test",
        "api_key": "test",
        "base_url": "http://localhost:8000",
        "timeout_seconds": 5,
    }

    # Test with URLError (should not raise, should return None)
    from urllib.error import URLError

    with patch("openbridge.opencode_bridge.urlopen") as mock_open:
        mock_open.side_effect = URLError("Connection failed")
        result = bridge._call_chat_completion(runtime, {"test": "payload"})
        assert result is None

    # Test with TimeoutError (should not raise, should return None)
    with patch("openbridge.opencode_bridge.urlopen") as mock_open:
        mock_open.side_effect = TimeoutError("Request timeout")
        result = bridge._call_chat_completion(runtime, {"test": "payload"})
        assert result is None

    # Test with OSError (should not raise, should return None)
    with patch("openbridge.opencode_bridge.urlopen") as mock_open:
        mock_open.side_effect = OSError("Network error")
        result = bridge._call_chat_completion(runtime, {"test": "payload"})
        assert result is None


def test_call_chat_completion_handles_invalid_json_response():
    """Verify _call_chat_completion handles invalid JSON in response."""
    cfg = bridge_config()
    bridge = OpenCodeBridge(cfg)

    runtime = {
        "model": "test",
        "api_key": "test",
        "base_url": "http://localhost:8000",
        "timeout_seconds": 5,
    }

    with patch("openbridge.opencode_bridge.urlopen") as mock_open:
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__.return_value = mock_response
        mock_response.__exit__.return_value = None
        mock_open.return_value = mock_response

        result = bridge._call_chat_completion(runtime, {"test": "payload"})
        # Should handle JSONDecodeError gracefully and return None
        assert result is None
