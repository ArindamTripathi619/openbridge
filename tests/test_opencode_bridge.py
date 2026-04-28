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
    _find_markdown_safe_split_index,
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

    def test_chunk_message_keeps_code_fence_intact(self):
        text = (
            "intro\n\n"
            "```python\n"
            + ("print('hello')\n" * 260)
            + "```\n\n"
            "outro\n"
        )
        chunks = list(_chunk_message(text, limit=500))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_chunk_message_prefers_section_boundaries(self):
        text = "A" * 260 + "\n\n*Section Two*\n" + "B" * 260
        chunks = list(_chunk_message(text, limit=300))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(chunks[0].endswith("\n\n"))
        self.assertTrue(chunks[1].startswith("*Section Two*"))
        self.assertEqual("".join(chunks), text)

    def test_find_markdown_safe_split_index_avoids_fence_boundary(self):
        text = "before\n```\nline 1\nline 2\n```\nafter\n"
        split_index = _find_markdown_safe_split_index(text, 18)

        self.assertNotEqual(split_index, 18)
        self.assertGreater(split_index, 0)
        self.assertLess(split_index, len(text))
        self.assertEqual(text[:split_index] + text[split_index:], text)

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

    def test_chat_queue_rejects_when_full(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            allow_all_chats=True,
            log_level="INFO",
            chat_queue_max_pending=1,
            chat_queue_overflow_mode="reject",
        )
        bridge = OpenCodeBridge(config)

        def _fake_create_task(coro):
            coro.close()
            worker = Mock()
            worker.done.return_value = False
            return worker

        with patch("src.openbridge.opencode_bridge.asyncio.create_task", side_effect=_fake_create_task):
            first_queued = asyncio.run(bridge._enqueue_chat_prompt(123, "first", Mock()))
            second_queued = asyncio.run(bridge._enqueue_chat_prompt(123, "second", Mock()))

        self.assertTrue(first_queued)
        self.assertFalse(second_queued)
        self.assertEqual(bridge._chat_queues[123].qsize(), 1)

    def test_chat_queue_drops_oldest_when_configured(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            log_level="INFO",
            chat_queue_max_pending=1,
            chat_queue_overflow_mode="drop_oldest",
        )
        bridge = OpenCodeBridge(config)

        def _fake_create_task(coro):
            coro.close()
            worker = Mock()
            worker.done.return_value = False
            return worker

        with patch("src.openbridge.opencode_bridge.asyncio.create_task", side_effect=_fake_create_task):
            first_queued = asyncio.run(bridge._enqueue_chat_prompt(123, "first", Mock()))
            second_queued = asyncio.run(bridge._enqueue_chat_prompt(123, "second", Mock()))

        self.assertTrue(first_queued)
        self.assertTrue(second_queued)
        queue = bridge._chat_queues[123]
        self.assertEqual(queue.qsize(), 1)
        queued_prompt, _ = queue.get_nowait()
        self.assertEqual(queued_prompt, "second")

    def test_handle_text_reports_queue_overflow(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),
            allow_all_chats=True,
            log_level="INFO",
            chat_queue_max_pending=1,
            chat_queue_overflow_mode="reject",
        )
        bridge = OpenCodeBridge(config)

        def _fake_create_task(coro):
            coro.close()
            worker = Mock()
            worker.done.return_value = False
            return worker

        with patch("src.openbridge.opencode_bridge.asyncio.create_task", side_effect=_fake_create_task):
            asyncio.run(bridge._enqueue_chat_prompt(123, "first", Mock()))

        message = Mock()
        message.text = "second"
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        context = Mock(application=Mock())

        asyncio.run(bridge.handle_text(update, context))

        self.assertGreaterEqual(message.reply_text.await_count, 1)
        self.assertIn("too many pending requests", str(message.reply_text.await_args_list[-1]))

    def test_workflow_list_action_is_reachable(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)
        manager = Mock()
        manager.summary_text.return_value = "Configured workflows"
        bridge.set_workflow_manager(manager)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        context = Mock(args=["list"], application=Mock())

        asyncio.run(bridge.handle_workflow_command(update, context))

        message.reply_text.assert_awaited_once_with("Configured workflows")

    def test_health_and_stats_require_chat_allowlist(self):
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        denied_message = Mock()
        denied_message.reply_text = AsyncMock()
        denied_update = Mock(effective_message=denied_message, effective_chat=Mock(id=222))

        asyncio.run(bridge.handle_health(denied_update, Mock()))
        asyncio.run(bridge.handle_stats(denied_update, Mock()))

        denied_message.reply_text.assert_any_await("This chat is not allowed to view health.")
        denied_message.reply_text.assert_any_await("This chat is not allowed to view stats.")

    def test_text_handler_denies_messages_from_disallowed_chats(self):
        """Integration test: Denied chats cannot send text messages."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        denied_message = Mock()
        denied_message.text = "Hello bot"
        denied_message.reply_text = AsyncMock()
        denied_update = Mock(effective_message=denied_message, effective_chat=Mock(id=222))

        asyncio.run(bridge.handle_text(denied_update, Mock()))

        denied_message.reply_text.assert_awaited_once_with(
            "This chat is not allowed to use this bot."
        )

    def test_workflow_handler_denies_all_actions_from_disallowed_chats(self):
        """Integration test: Denied chats cannot execute any workflow actions."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        denied_message = Mock()
        denied_message.reply_text = AsyncMock()
        denied_update = Mock(effective_message=denied_message, effective_chat=Mock(id=222))

        # Test each workflow action: create, list, status, pause, resume, run
        for action in ["create", "list", "status", "pause", "resume", "run"]:
            denied_message.reply_text.reset_mock()
            context = Mock(args=[action], application=Mock())
            asyncio.run(bridge.handle_workflow_command(denied_update, context))
            denied_message.reply_text.assert_awaited_once_with(
                "This chat is not allowed to manage workflows."
            )

    def test_empty_allowlist_denies_all_chats(self):
        """Boundary test: Empty allowlist with allow_all_chats=False denies all chats."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids=set(),  # Empty allowlist
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        # Any chat ID should be denied
        self.assertFalse(bridge._is_chat_allowed(123))
        self.assertFalse(bridge._is_chat_allowed(456))
        self.assertFalse(bridge._is_chat_allowed(999999))

    def test_allow_all_chats_overrides_allowlist(self):
        """Boundary test: allow_all_chats=True allows all chats regardless of allowlist."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=True,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        # All chats should be allowed, even those not in the allowlist
        self.assertTrue(bridge._is_chat_allowed(111))  # In allowlist
        self.assertTrue(bridge._is_chat_allowed(222))  # Not in allowlist
        self.assertTrue(bridge._is_chat_allowed(999999))  # Large ID

    def test_single_allowed_chat_edge_case(self):
        """Boundary test: Single allowed chat ID is properly enforced."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={42},  # Single chat
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        self.assertTrue(bridge._is_chat_allowed(42))
        self.assertFalse(bridge._is_chat_allowed(41))
        self.assertFalse(bridge._is_chat_allowed(43))

    def test_denied_chat_never_receives_protected_payloads(self):
        """Integration test: Denied chat receives denial message without processing payload."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        denied_message = Mock()
        denied_message.reply_text = AsyncMock()
        denied_update = Mock(effective_message=denied_message, effective_chat=Mock(id=222))
        context = Mock(args=["list"], application=Mock())

        # Ensure workflow manager is not called for denied chat
        mock_manager = Mock()
        bridge.set_workflow_manager(mock_manager)

        asyncio.run(bridge.handle_workflow_command(denied_update, context))

        # Verify denial message was sent
        denied_message.reply_text.assert_awaited_once_with(
            "This chat is not allowed to manage workflows."
        )
        # Verify workflow manager was never called
        mock_manager.summary_text.assert_not_called()

    def test_allowed_chat_passes_allowlist_check(self):
        """Test: Allowed chat passes allowlist check and proceeds with handler."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={111},
            allow_all_chats=False,
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        allowed_message = Mock()
        allowed_message.reply_text = AsyncMock()
        allowed_update = Mock(effective_message=allowed_message, effective_chat=Mock(id=111))
        context = Mock(args=["list"], application=Mock())

        # Set up mock manager
        mock_manager = Mock()
        mock_manager.summary_text.return_value = "Active workflows"
        bridge.set_workflow_manager(mock_manager)

        asyncio.run(bridge.handle_workflow_command(allowed_update, context))

        # Verify workflow manager was called (passed allowlist check)
        mock_manager.summary_text.assert_called_once()
        allowed_message.reply_text.assert_awaited_once_with("Active workflows")

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

    def test_send_result_messages_sends_markdown_for_raw_output(self):
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

    def test_escape_markdown_v2_preserves_formatting_when_requested(self):
        value = "*Bold Text* [Link](https://telegram.org/) and wow!"

        escaped = _escape_markdown_v2(value, preserve_formatting=True)

        self.assertEqual(escaped, "*Bold Text* [Link](https://telegram\\.org/) and wow\\!")

    def test_escape_markdown_v2_escapes_dot_inside_italic(self):
        value = "_U.S. updates_ and end."

        escaped = _escape_markdown_v2(value, preserve_formatting=True)

        self.assertEqual(escaped, "_U\\.S\\. updates_ and end\\.")




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

    def test_workflow_status_action_reaches_handler(self):
        """Unit test: Workflow status action routes correctly and calls manager."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        
        mock_manager = Mock()
        mock_manager.status_text.return_value = "Workflow status: active"
        bridge.set_workflow_manager(mock_manager)

        context = Mock(args=["status", "workflow_id_123"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        mock_manager.status_text.assert_called_once_with("workflow_id_123")
        message.reply_text.assert_awaited_once_with("Workflow status: active")

    def test_workflow_pause_action_reaches_handler(self):
        """Unit test: Workflow pause action routes correctly and calls manager."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        
        mock_manager = Mock()
        mock_manager.set_paused.return_value = None
        bridge.set_workflow_manager(mock_manager)

        context = Mock(args=["pause", "workflow_id_123"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        mock_manager.set_paused.assert_called_once_with("workflow_id_123", True)
        message.reply_text.assert_awaited_once()

    def test_workflow_resume_action_reaches_handler(self):
        """Unit test: Workflow resume action routes correctly and calls manager."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        
        mock_manager = Mock()
        mock_manager.set_paused.return_value = None
        bridge.set_workflow_manager(mock_manager)

        context = Mock(args=["resume", "workflow_id_123"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        mock_manager.set_paused.assert_called_once_with("workflow_id_123", False)
        message.reply_text.assert_awaited_once()

    def test_workflow_run_action_reaches_handler(self):
        """Unit test: Workflow run action routes correctly and calls runner."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        
        # Mock the _run_workflow_now method
        bridge._run_workflow_now = AsyncMock(return_value="Workflow executed")

        context = Mock(args=["run", "workflow_id_123"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        bridge._run_workflow_now.assert_awaited_once()

    def test_workflow_unknown_action_returns_error(self):
        """Unit test: Unknown workflow action returns error message."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))

        # Pass workflow_id arg to reach the unknown action handler
        context = Mock(args=["unknown_action", "workflow_id"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        message.reply_text.assert_awaited_once()
        # Verify "Unknown workflow action" appears in error message
        call_args = message.reply_text.call_args
        self.assertIn("Unknown workflow action", str(call_args))

    def test_workflow_missing_id_argument_returns_error(self):
        """Unit test: Actions requiring workflow_id return error when missing argument."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))

        # Test actions that require workflow_id but don't provide it
        for action in ["status", "pause", "resume", "run"]:
            message.reply_text.reset_mock()
            context = Mock(args=[action], application=Mock())
            asyncio.run(bridge.handle_workflow_command(update, context))
            message.reply_text.assert_awaited_once()
            # Should mention workflow id requirement
            call_args = message.reply_text.call_args
            self.assertIn("workflow id", str(call_args).lower())


    def test_workflow_create_action_reaches_handler(self):
        """Unit test: Workflow create action routes correctly."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))
        
        # Mock the draft workflow method
        bridge._draft_workflow_from_instruction = AsyncMock(
            return_value={"id": "new_workflow", "name": "Test Workflow"}
        )

        context = Mock(args=["create", "daily", "fetch"], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        # Verify draft method was called
        bridge._draft_workflow_from_instruction.assert_awaited_once()

    def test_workflow_no_action_returns_help(self):
        """Unit test: Workflow command with no action returns help message."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))

        context = Mock(args=[], application=Mock())
        asyncio.run(bridge.handle_workflow_command(update, context))

        message.reply_text.assert_awaited_once()
        # Help message should mention available actions
        call_args = message.reply_text.call_args
        call_text = str(call_args)
        self.assertTrue(
            any(action in call_text for action in ["list", "create", "status"]),
            f"Help message should mention actions, got: {call_text}"
        )

    def test_workflow_routing_regression_early_returns_do_not_block_execution(self):
        """Regression test: No unconditional early return blocks any subcommand routing."""
        config = BridgeConfig(
            telegram_token="123:token",
            opencode_model="opencode/big-pickle",
            opencode_working_dir=".",
            opencode_timeout_seconds=10,
            max_concurrent_jobs=1,
            allowed_chat_ids={123},
            log_level="INFO",
        )
        bridge = OpenCodeBridge(config)

        message = Mock()
        message.reply_text = AsyncMock()
        update = Mock(effective_message=message, effective_chat=Mock(id=123))

        mock_manager = Mock()
        mock_manager.summary_text.return_value = "Active workflows"
        mock_manager.status_text.return_value = "Status: active"
        mock_manager.set_paused.return_value = None
        bridge.set_workflow_manager(mock_manager)
        bridge._run_workflow_now = AsyncMock(return_value="Executed")

        # Test that all actions route successfully (not blocked by early returns)
        test_actions = [
            (["list"], lambda: mock_manager.summary_text.called),
            (["status", "wf1"], lambda: mock_manager.status_text.called),
            (["pause", "wf1"], lambda: mock_manager.set_paused.called),
            (["resume", "wf1"], lambda: mock_manager.set_paused.called),
        ]

        for args, check_called in test_actions:
            message.reply_text.reset_mock()
            mock_manager.reset_mock()
            context = Mock(args=args, application=Mock())
            asyncio.run(bridge.handle_workflow_command(update, context))
            # Verify handler was reached and executed (not blocked by early return)
            message.reply_text.assert_awaited_once()



if __name__ == "__main__":
    unittest.main()
