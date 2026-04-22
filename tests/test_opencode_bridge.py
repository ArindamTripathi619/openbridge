import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.telewatch.opencode_bridge import (
    BridgeConfig,
    OpenCodeBridge,
    _chunk_message,
    _extract_session_id,
    _extract_text_candidates,
    _redact_sensitive_text,
)


class TestOpenCodeBridgeHelpers(unittest.TestCase):
    def test_run_prompt_uses_api_session_successfully(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)
        bridge._get_or_create_session = AsyncMock(return_value="session-1")
        bridge._run_prompt_via_api_sync = lambda session_id, prompt: "api result"

        result = asyncio.run(bridge.run_prompt(123, "test prompt"))

        self.assertEqual(result, "api result")
        self.assertEqual(bridge._stats["successful_requests"], 1)

    def test_redacts_telegram_token_in_request_logs(self):
        text = "HTTP Request: POST https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890/getUpdates"

        redacted = _redact_sensitive_text(text)

        self.assertNotIn("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_decorate_output_builds_html_messages(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
            decorator_enabled=True,
            decorator_api_key="sk-test",
            decorator_model="free-model",
            decorator_base_url="https://example.test/v1",
        )
        bridge = OpenCodeBridge(config)
        bridge._decorate_output_sync = lambda raw: {
            "title": "Summary",
            "summary": "Short decorated summary.",
            "highlights": ["First highlight"],
            "actions": ["Next action"],
            "warnings": ["One warning"],
        }

        messages = asyncio.run(bridge.decorate_output("raw output"))

        self.assertIsNotNone(messages)
        self.assertTrue(any("Summary" in message for message in messages))
        self.assertTrue(any("Highlights" in message for message in messages))
        self.assertTrue(any("Actions" in message for message in messages))
        self.assertTrue(any("Warnings" in message for message in messages))

    def test_enhance_prompt_uses_input_llm_when_enabled(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
            input_llm_enabled=True,
            input_llm_provider="litellm",
            input_llm_model="groq-gpt-oss-mini",
            input_llm_litellm_port=8000,
        )
        bridge = OpenCodeBridge(config)
        bridge._enhance_prompt_sync = lambda runtime, raw: "Refined prompt for OpenCode"

        enhanced = asyncio.run(bridge.enhance_prompt("raw user message"))

        self.assertEqual(enhanced, "Refined prompt for OpenCode")

    def test_enhance_prompt_falls_back_to_original_on_failure(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
            input_llm_enabled=True,
            input_llm_provider="litellm",
            input_llm_model="groq-gpt-oss-mini",
            input_llm_litellm_port=8000,
        )
        bridge = OpenCodeBridge(config)

        def _boom(runtime, raw):
            raise RuntimeError("failed")

        bridge._enhance_prompt_sync = _boom

        enhanced = asyncio.run(bridge.enhance_prompt("original prompt"))

        self.assertEqual(enhanced, "original prompt")

    def test_extract_session_id_handles_nested_payload(self):
        payload = {"data": {"session": {"id": "abc-123"}}}
        self.assertEqual(_extract_session_id(payload), "abc-123")

    def test_chunk_message_splits_long_text(self):
        text = "a" * 8000
        chunks = list(_chunk_message(text, limit=3000))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 3000 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_run_prompt_handles_api_failure(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)
        bridge._get_or_create_session = AsyncMock(return_value="session-1")

        def _fail(session_id, prompt):
            raise RuntimeError("boom")

        bridge._run_prompt_via_api_sync = _fail

        result = asyncio.run(bridge.run_prompt(123, "hello"))

        self.assertIn("OpenCode API request failed", result)
        self.assertEqual(bridge._stats["failed_requests"], 1)

    def test_send_session_message_prefers_parts_payload(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        captured_payloads = []

        def _fake_request(method, path, payload=None):
            captured_payloads.append(payload)
            return {"text": "ok"}

        bridge._opencode_request_sync = _fake_request

        result = bridge._send_session_message_sync("session-1", "Hello")

        self.assertEqual(result, "ok")
        self.assertEqual(captured_payloads[0], {"parts": [{"type": "text", "text": "Hello"}]})

    def test_extract_text_candidates_from_parts_payload(self):
        payload = {
            "messages": [
                {
                    "role": "assistant",
                    "parts": [
                        {"type": "text", "text": "Hello from assistant"},
                    ],
                }
            ]
        }

        candidates = _extract_text_candidates(payload)
        self.assertIn("Hello from assistant", candidates)


if __name__ == "__main__":
    unittest.main()
