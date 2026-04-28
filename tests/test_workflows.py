import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.openbridge.opencode_bridge import BridgeConfig, OpenCodeBridge
from src.openbridge.workflows import (
    WorkflowManager,
    WorkflowStep,
    WorkflowState,
    WorkflowDefinition,
    _is_safe_fetch_url,
    _next_run_timestamp,
    _normalize_http_payload,
    load_workflows,
    save_workflows,
    sample_workflows,
)


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeBridge:
    async def run_prompt(self, chat_id, prompt):
        return f"workflow:{chat_id}:{prompt}"


class CaptureBridge:
    def __init__(self):
        self.prompts = []

    async def run_prompt(self, chat_id, prompt):
        self.prompts.append((chat_id, prompt))
        return "ok"


class TestWorkflows(unittest.TestCase):
    def test_load_sample_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            save_workflows(workflows_file, sample_workflows())

            workflows = load_workflows(workflows_file)

            self.assertEqual(len(workflows), 1)
            self.assertEqual(workflows[0].id, "daily_news_digest")
            self.assertEqual(workflows[0].steps[0].type, "http_fetch")
            self.assertEqual(workflows[0].targets, [0])

    def test_run_workflow_sends_telegram_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            state_file = Path(temp_dir) / "state.json"
            save_workflows(
                workflows_file,
                {
                    "workflows": [
                        {
                            "id": "daily_digest",
                            "name": "Daily Digest",
                            "enabled": True,
                            "schedule": "daily@06:55",
                            "targets": [123456789],
                            "steps": [
                                {
                                    "type": "opencode_prompt",
                                    "prompt_template": "Summarize this text:\n\n{input}",
                                },
                                {"type": "telegram_send"},
                            ],
                        }
                    ]
                },
            )

            config = BridgeConfig.from_mapping(
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "60",
                    "OPENCODE_MAX_CONCURRENT": "1",
                }
            )
            manager = WorkflowManager(
                config=config,
                bridge=FakeBridge(),
                workflows_file=workflows_file,
                state_file=state_file,
            )
            bot = FakeBot()

            result = asyncio.run(manager.run_workflow("daily_digest", telegram_bot=bot, manual=True))

            self.assertEqual(result.status, "success")
            self.assertEqual(len(bot.messages), 1)
            self.assertEqual(bot.messages[0]["chat_id"], 123456789)
            self.assertIn("workflow:", bot.messages[0]["text"])
            self.assertTrue(state_file.exists())

    def test_run_workflow_rejects_oversized_generated_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            state_file = Path(temp_dir) / "state.json"
            save_workflows(
                workflows_file,
                {
                    "workflows": [
                        {
                            "id": "prompt_guard",
                            "name": "Prompt Guard",
                            "enabled": True,
                            "schedule": "daily@06:55",
                            "targets": [123456789],
                            "steps": [
                                {"type": "http_fetch", "sources": ["https://example.com/news"]},
                                {"type": "opencode_prompt", "prompt_template": "Summarize:\n\n{input}"},
                            ],
                        }
                    ]
                },
            )

            config = BridgeConfig.from_mapping(
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "60",
                    "OPENCODE_MAX_CONCURRENT": "1",
                    "OPENBRIDGE_WORKFLOW_PROMPT_MAX_CHARS": "20",
                    "OPENBRIDGE_WORKFLOW_PROMPT_OVERFLOW_MODE": "reject",
                }
            )
            bridge = CaptureBridge()
            manager = WorkflowManager(
                config=config,
                bridge=bridge,
                workflows_file=workflows_file,
                state_file=state_file,
            )

            async def fake_http_fetch(step):
                return "x" * 200

            manager._run_http_fetch_step = fake_http_fetch  # type: ignore[method-assign]

            result = asyncio.run(manager.run_workflow("prompt_guard", telegram_bot=None, manual=True))

            self.assertEqual(result.status, "failed")
            self.assertIn("prompt input is too large", result.error or "")
            self.assertEqual(bridge.prompts, [])

    def test_run_workflow_truncates_oversized_generated_prompt_when_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            state_file = Path(temp_dir) / "state.json"
            save_workflows(
                workflows_file,
                {
                    "workflows": [
                        {
                            "id": "prompt_guard",
                            "name": "Prompt Guard",
                            "enabled": True,
                            "schedule": "daily@06:55",
                            "targets": [123456789],
                            "steps": [
                                {"type": "http_fetch", "sources": ["https://example.com/news"]},
                                {"type": "opencode_prompt", "prompt_template": "Summarize:\n\n{input}"},
                            ],
                        }
                    ]
                },
            )

            config = BridgeConfig.from_mapping(
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "60",
                    "OPENCODE_MAX_CONCURRENT": "1",
                    "OPENBRIDGE_WORKFLOW_PROMPT_MAX_CHARS": "20",
                    "OPENBRIDGE_WORKFLOW_PROMPT_OVERFLOW_MODE": "truncate",
                }
            )
            bridge = CaptureBridge()
            manager = WorkflowManager(
                config=config,
                bridge=bridge,
                workflows_file=workflows_file,
                state_file=state_file,
            )

            async def fake_http_fetch(step):
                return "x" * 200

            manager._run_http_fetch_step = fake_http_fetch  # type: ignore[method-assign]

            result = asyncio.run(manager.run_workflow("prompt_guard", telegram_bot=None, manual=True))

            self.assertEqual(result.status, "success")
            self.assertEqual(len(bridge.prompts), 1)
            _, prompt = bridge.prompts[0]
            self.assertLessEqual(len(prompt), 20)
            self.assertIn("…", prompt)

    def test_validate_rejects_invalid_workflow_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            workflows_file.write_text("not-json", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_workflows(workflows_file)

    def test_cron_schedule_next_run(self):
        workflow = WorkflowDefinition(
            id="cron_job",
            name="Cron Job",
            schedule="cron:*/5 * * * *",
            steps=[WorkflowStep(type="transform_python")],
        )
        state = WorkflowState()

        next_run = _next_run_timestamp(workflow, state, now=datetime(2026, 4, 22, 10, 1, 10).timestamp())

        self.assertIsNotNone(next_run)
        assert next_run is not None
        self.assertEqual(datetime.fromtimestamp(next_run).minute % 5, 0)

    def test_http_normalization_for_rss_payload(self):
        rss = """
        <rss version=\"2.0\">
          <channel>
            <title>News</title>
            <item>
              <title>Headline One</title>
              <link>https://example.com/1</link>
              <pubDate>Wed, 22 Apr 2026 06:00:00 GMT</pubDate>
              <description><![CDATA[<p>Summary one</p>]]></description>
            </item>
            <item>
              <title>Headline Two</title>
              <link>https://example.com/2</link>
              <description>Summary two</description>
            </item>
          </channel>
        </rss>
        """

        normalized = _normalize_http_payload(
            "https://example.com/rss.xml",
            rss,
            content_type="application/rss+xml",
            normalize_mode="auto",
            max_items=5,
        )

        self.assertIn("Feed items: 2", normalized)
        self.assertIn("Headline One", normalized)
        self.assertIn("Summary one", normalized)

    def test_pause_and_resume_workflow_persists_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            state_file = Path(temp_dir) / "state.json"
            save_workflows(
                workflows_file,
                {
                    "workflows": [
                        {
                            "id": "daily_digest",
                            "name": "Daily Digest",
                            "enabled": True,
                            "schedule": "daily@06:55",
                            "targets": [123456789],
                            "steps": [{"type": "transform_python", "mode": "identity"}],
                        }
                    ]
                },
            )

            config = BridgeConfig.from_mapping(
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "60",
                    "OPENCODE_MAX_CONCURRENT": "1",
                }
            )
            manager = WorkflowManager(
                config=config,
                bridge=FakeBridge(),
                workflows_file=workflows_file,
                state_file=state_file,
            )

            manager.set_paused("daily_digest", True)
            paused_text = manager.status_text("daily_digest")
            self.assertIn("Paused: yes", paused_text)

            manager.set_paused("daily_digest", False)
            resumed_text = manager.status_text("daily_digest")
            self.assertIn("Paused: no", resumed_text)

    def test_safe_fetch_url_blocks_private_addresses(self):
        self.assertFalse(_is_safe_fetch_url("http://127.0.0.1:8080"))
        self.assertFalse(_is_safe_fetch_url("http://169.254.169.254/latest/meta-data/"))

    @patch("src.openbridge.workflows.socket.getaddrinfo", side_effect=OSError("dns failed"))
    def test_safe_fetch_url_denies_dns_resolution_errors(self, _mock_getaddrinfo):
        self.assertFalse(_is_safe_fetch_url("https://example.invalid/path"))

    def test_workflow_validation_rejects_excessive_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = BridgeConfig.from_mapping(
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "60",
                    "OPENCODE_MAX_CONCURRENT": "1",
                }
            )
            bridge = OpenCodeBridge(config)
            workflow = {
                "id": "too_many_steps",
                "name": "Too Many Steps",
                "enabled": True,
                "schedule": "daily@06:55",
                "targets": [123456789],
                "steps": [{"type": "telegram_send"}] * 11,
            }

            errors = bridge._validate_workflow_safety(workflow, 123456789)

            self.assertTrue(errors)
            self.assertTrue(any("more than 10 steps" in error for error in errors))


    def test_save_workflows_applies_restrictive_file_permissions(self):
        """Test: save_workflows() applies 0600 permissions to workflow file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            payload = sample_workflows()

            save_workflows(workflows_file, payload)

            # Verify file exists
            self.assertTrue(workflows_file.exists())

            # Verify file has restrictive permissions (rw-------)
            file_stat = workflows_file.stat()
            file_mode = file_stat.st_mode & 0o777
            self.assertEqual(file_mode, 0o600, f"Expected file mode 0o600, got {oct(file_mode)}")

            # Verify parent directory has 0o700 permissions (rwx------)
            parent_stat = workflows_file.parent.stat()
            parent_mode = parent_stat.st_mode & 0o777
            self.assertEqual(parent_mode, 0o700, f"Expected parent mode 0o700, got {oct(parent_mode)}")

    def test_workflow_state_save_applies_restrictive_file_permissions(self):
        """Test: WorkflowStateStore.save() applies 0600 permissions to state file."""
        from src.openbridge.workflows import WorkflowStateStore

        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "workflows-state.json"
            store = WorkflowStateStore(state_file)

            # Add a workflow state and save
            store.get("test_workflow").run_count = 5
            store.save()

            # Verify file exists
            self.assertTrue(state_file.exists())

            # Verify file has restrictive permissions (rw-------)
            file_stat = state_file.stat()
            file_mode = file_stat.st_mode & 0o777
            self.assertEqual(file_mode, 0o600, f"Expected file mode 0o600, got {oct(file_mode)}")

            # Verify parent directory has 0o700 permissions (rwx------)
            parent_stat = state_file.parent.stat()
            parent_mode = parent_stat.st_mode & 0o777
            self.assertEqual(parent_mode, 0o700, f"Expected parent mode 0o700, got {oct(parent_mode)}")


if __name__ == "__main__":
    unittest.main()
