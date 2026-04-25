import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import AsyncMock, Mock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.openbridge.opencode_bridge import (
    BridgeConfig,
    OpenCodeBridge,
    run_bridge,
    _chunk_message,
    _escape_markdown_v2,
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
        bridge._decorate_output_sync = lambda raw_output: {
            "title": "Summary",
            "summary": "Short decorated summary.",
            "highlights": ["First highlight"],
            "actions": ["Next action"],
            "warnings": ["One warning"],
        }

        messages = asyncio.run(bridge.decorate_output("raw output"))

        self.assertIsNotNone(messages)
        assert messages is not None
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
        bridge._enhance_prompt_sync = lambda runtime, raw_prompt: "Refined prompt for OpenCode"

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

        def _boom(runtime, raw_prompt):
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

        self.assertIn("Check logs for details", result)
        self.assertEqual(bridge._stats["failed_requests"], 1)

    def test_allow_all_chats_is_explicit(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": ".",
                "OPENCODE_TIMEOUT_SECONDS": "10",
                "OPENCODE_MAX_CONCURRENT": "1",
                "TELEGRAM_ALLOW_ALL_CHATS": "1",
            }
        )
        bridge = OpenCodeBridge(config)

        self.assertTrue(bridge._is_chat_allowed(999999))

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

    def test_send_result_messages_sends_escaped_markdown_for_raw_output(self):
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
        bridge.decorate_output = AsyncMock(return_value=None)

        bot = AsyncMock()
        app = Mock(bot=bot)

        asyncio.run(bridge._send_result_messages(123, "Hello, world!", app))

        bot.send_message.assert_awaited_once_with(
            chat_id=123,
            text="Hello, world\\!",
            parse_mode="MarkdownV2",
        )

    def test_escape_markdown_v2_preserves_code_and_escapes_plain_text(self):
        value = "a.b! `x.y!`\\n```text\\nq.w!\\n``` and \\\\"

        escaped = _escape_markdown_v2(value)

        self.assertEqual(escaped, "a\\.b\\! `x.y!`\\n```text\\nq.w!\\n``` and \\\\")

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

    def test_extract_json_object_text_from_fenced_output(self):
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

        text = """```json
        {"id":"daily_digest","schedule":"daily@06:55","steps":[{"type":"telegram_send"}]}
        ```"""
        extracted = bridge._extract_json_object_text(text)

        self.assertIsNotNone(extracted)
        parsed = json.loads(str(extracted))
        self.assertEqual(parsed["id"], "daily_digest")

    def test_pending_workflow_reply_yes_saves_workflow_file(self):
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
        chat_id = 123
        bridge._pending_workflow_drafts[chat_id] = {
            "workflow": {
                "id": "daily_digest",
                "name": "Daily Digest",
                "enabled": True,
                "timezone": "local",
                "schedule": "daily@06:55",
                "targets": [123],
                "steps": [
                    {"type": "transform_python", "mode": "identity"},
                    {"type": "telegram_send"},
                ],
                "retry_policy": {},
                "dedupe_policy": {},
                "metadata": {},
            }
        }

        class FakeApp:
            bot = None

        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_file = Path(temp_dir) / "workflows.json"
            original = OpenCodeBridge._workflow_file_path
            OpenCodeBridge._workflow_file_path = staticmethod(lambda: workflow_file)
            try:
                reply = asyncio.run(bridge._handle_pending_workflow_reply(chat_id, "YES", FakeApp()))
            finally:
                OpenCodeBridge._workflow_file_path = original

            self.assertIsNotNone(reply)
            self.assertTrue(workflow_file.exists())
            payload = json.loads(workflow_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["workflows"][0]["id"], "daily_digest")
            self.assertNotIn(chat_id, bridge._pending_workflow_drafts)

    def test_run_bridge_accepts_stop_event(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
        )
        fake_app = Mock()
        fake_app.run_polling = Mock()
        fake_app.stop_running = Mock()

        with patch("src.openbridge.opencode_bridge.configure_logging"), patch(
            "src.openbridge.opencode_bridge.OpenCodeBridge", return_value=Mock()
        ), patch("src.openbridge.opencode_bridge.build_application", return_value=fake_app):
            run_bridge(config, workflow_manager=Mock(), stop_event=Event())

        fake_app.run_polling.assert_called_once_with(close_loop=False, stop_signals=None)


if __name__ == "__main__":
    unittest.main()
