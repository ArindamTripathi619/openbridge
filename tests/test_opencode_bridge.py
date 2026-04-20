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
    _clean_opencode_output,
    _model_candidates,
    _redact_sensitive_text,
)


class TestOpenCodeBridgeHelpers(unittest.TestCase):
    def test_model_candidates_keep_primary_first(self):
        candidates = _model_candidates("opencode/big-pickle")

        self.assertEqual(
            candidates,
            ["opencode/big-pickle", "opencode/minimax-m2.5-free", "opencode/nemotron-3-super-free"],
        )

    def test_run_prompt_retries_free_models_after_quota(self):
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
        bridge._run_prompt_once = AsyncMock(
            side_effect=[
                "__QUOTA__:quota exceeded for opencode/big-pickle",
                "__QUOTA__:quota exceeded for opencode/minimax-m2.5-free",
                "fallback result from opencode/nemotron-3-super-free",
            ]
        )

        result = asyncio.run(bridge.run_prompt("test prompt"))

        self.assertEqual(result, "fallback result from opencode/nemotron-3-super-free")
        self.assertEqual(bridge._run_prompt_once.await_count, 3)

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

    def test_clean_opencode_output_strips_tool_noise(self):
        raw = """$ opencode run --model opencode/big-pickle "explain pid 112527"
✱ running tool: ps
> fetched process table

I'll investigate PID 112527 to see what it's doing.
## Summary for PID 112527

**Process Info:**
- **Command:** `.venv/bin/python main.py`
"""

        cleaned = _clean_opencode_output(raw)

        self.assertIn("I'll investigate PID 112527", cleaned)
        self.assertIn("## Summary for PID 112527", cleaned)
        self.assertNotIn("✱ running tool", cleaned)

    def test_chunk_message_splits_long_text(self):
        text = "a" * 8000
        chunks = list(_chunk_message(text, limit=3000))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 3000 for chunk in chunks))
        self.assertEqual("".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
