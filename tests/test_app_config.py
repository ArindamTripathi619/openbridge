import contextlib
import io
import tempfile
import unittest
from pathlib import Path
import os
import sys
from unittest.mock import Mock, patch, call

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.openbridge.app import (
    OPENCODE_CONFIG_FILE,
    _build_opencode_systemd_unit,
    _build_systemd_unit,
    _install_missing_dependencies,
    deploy_validate_command,
    _load_banner_text,
    _load_pid,
    _merged_config,
    _missing_dependencies,
    _sync_opencode_env_from_bridge_config,
    _show_banner,
    build_parser,
    get_resource_path,
    is_process_alive,
    read_env_file,
    render_systemd_command,
    write_env_file,
    workflows_init_command,
)
from src.openbridge.opencode_bridge import BridgeConfig


class TestAppConfig(unittest.TestCase):
    def test_env_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            original = {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENCODE_API_BASE_URL": "http://127.0.0.1:4096",
                "OPENCODE_API_USERNAME": "opencode",
                "OPENCODE_API_PASSWORD": "pw",
                "OPENCODE_API_TIMEOUT_SECONDS": "120",
                "OPENCODE_SERVER_USERNAME": "server-user",
                "OPENCODE_SERVER_PASSWORD": "server-pw",
                "TELEGRAM_ALLOWED_CHAT_IDS": "123,456",
                "LOG_LEVEL": "DEBUG",
                "OPENBRIDGE_INPUT_LLM_ENABLED": "1",
                "OPENBRIDGE_INPUT_LLM_PROVIDER": "litellm",
                "OPENBRIDGE_INPUT_LLM_MODEL": "groq-gpt-oss-mini",
            }

            write_env_file(config_path, original)
            loaded = read_env_file(config_path)

            self.assertEqual(loaded["TELEGRAM_BOT_TOKEN"], original["TELEGRAM_BOT_TOKEN"])
            self.assertEqual(loaded["OPENCODE_MODEL"], original["OPENCODE_MODEL"])
            self.assertEqual(loaded["TELEGRAM_ALLOWED_CHAT_IDS"], original["TELEGRAM_ALLOWED_CHAT_IDS"])
            self.assertEqual(loaded["LOG_LEVEL"], original["LOG_LEVEL"])
            self.assertEqual(loaded["OPENBRIDGE_INPUT_LLM_PROVIDER"], "litellm")
            self.assertEqual(loaded["OPENCODE_API_BASE_URL"], "http://127.0.0.1:4096")

    def test_bridge_config_from_mapping(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENCODE_API_BASE_URL": "http://127.0.0.1:4096",
                "OPENCODE_API_USERNAME": "opencode",
                "OPENCODE_API_PASSWORD": "pw",
                "OPENCODE_API_TIMEOUT_SECONDS": "150",
                "OPENCODE_SERVER_USERNAME": "server-user",
                "OPENCODE_SERVER_PASSWORD": "server-pw",
                "TELEGRAM_ALLOWED_CHAT_IDS": "123,456",
                "LOG_LEVEL": "info",
            }
        )

        self.assertEqual(config.telegram_token, "123:token")
        self.assertEqual(config.opencode_model, "opencode/big-pickle")
        self.assertEqual(config.opencode_working_dir, "/tmp/project")
        self.assertEqual(config.opencode_timeout_seconds, 600)
        self.assertEqual(config.max_concurrent_jobs, 1)
        self.assertEqual(config.opencode_api_base_url, "http://127.0.0.1:4096")
        self.assertEqual(config.opencode_api_username, "opencode")
        self.assertEqual(config.opencode_api_password, "pw")
        self.assertEqual(config.opencode_api_timeout_seconds, 150)
        self.assertEqual(config.allowed_chat_ids, {123, 456})
        self.assertFalse(config.allow_all_chats)
        self.assertEqual(config.log_level, "INFO")

    def test_bridge_config_parses_workflow_prompt_guardrails(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENBRIDGE_WORKFLOW_PROMPT_MAX_CHARS": "2048",
                "OPENBRIDGE_WORKFLOW_PROMPT_OVERFLOW_MODE": "truncate",
            }
        )

        self.assertEqual(config.workflow_prompt_max_chars, 2048)
        self.assertEqual(config.workflow_prompt_overflow_mode, "truncate")

    def test_bridge_config_parses_chat_queue_guardrails(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENBRIDGE_CHAT_QUEUE_MAX_PENDING": "7",
                "OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE": "drop_oldest",
            }
        )

        self.assertEqual(config.chat_queue_max_pending, 7)
        self.assertEqual(config.chat_queue_overflow_mode, "drop_oldest")

    def test_bridge_config_denies_all_chats_by_default(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
            }
        )

        self.assertEqual(config.allowed_chat_ids, set())
        self.assertFalse(config.allow_all_chats)

    def test_bridge_config_allows_all_chats_when_explicitly_enabled(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "TELEGRAM_ALLOW_ALL_CHATS": "1",
            }
        )

        self.assertTrue(config.allow_all_chats)

    def test_bridge_config_parses_input_and_output_llm_roles(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENBRIDGE_INPUT_LLM_ENABLED": "1",
                "OPENBRIDGE_INPUT_LLM_PROVIDER": "litellm",
                "OPENBRIDGE_INPUT_LLM_MODEL": "groq-gpt-oss-mini",
                "OPENBRIDGE_INPUT_LLM_LITELLM_PORT": "8000",
                "OPENBRIDGE_OUTPUT_LLM_ENABLED": "1",
                "OPENBRIDGE_OUTPUT_LLM_PROVIDER": "api",
                "OPENBRIDGE_OUTPUT_LLM_API_KEY": "sk-test",
                "OPENBRIDGE_OUTPUT_LLM_MODEL": "some-model",
                "OPENBRIDGE_OUTPUT_LLM_BASE_URL": "https://example.test/v1",
            }
        )

        self.assertTrue(config.input_llm_enabled)
        self.assertEqual(config.input_llm_provider, "litellm")
        self.assertEqual(config.input_llm_model, "groq-gpt-oss-mini")
        self.assertEqual(config.input_llm_litellm_port, 8000)
        self.assertTrue(config.output_llm_enabled)
        self.assertEqual(config.output_llm_provider, "api")
        self.assertEqual(config.output_llm_api_key, "sk-test")

    def test_bridge_config_accepts_legacy_telewatch_keys(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "TELEWATCH_INPUT_LLM_ENABLED": "1",
                "TELEWATCH_INPUT_LLM_PROVIDER": "litellm",
                "TELEWATCH_INPUT_LLM_MODEL": "groq-gpt-oss-mini",
                "TELEWATCH_OUTPUT_LLM_ENABLED": "1",
                "TELEWATCH_OUTPUT_LLM_PROVIDER": "api",
                "TELEWATCH_OUTPUT_LLM_API_KEY": "sk-test",
                "TELEWATCH_OUTPUT_LLM_MODEL": "some-model",
                "TELEWATCH_OUTPUT_LLM_BASE_URL": "https://example.test/v1",
            }
        )

        self.assertTrue(config.input_llm_enabled)
        self.assertEqual(config.input_llm_provider, "litellm")
        self.assertEqual(config.input_llm_model, "groq-gpt-oss-mini")
        self.assertTrue(config.output_llm_enabled)
        self.assertEqual(config.output_llm_provider, "api")
        self.assertEqual(config.output_llm_api_key, "sk-test")

    def test_build_systemd_unit_includes_restart_policy(self):
        unit_text = _build_systemd_unit(Path("/home/DevCrewX/Projects/TelegramRemoteProgressBot"))

        self.assertIn("Restart=on-failure", unit_text)
        self.assertIn("RestartSec=5s", unit_text)
        self.assertIn("TimeoutStartSec=2min", unit_text)
        self.assertIn("TimeoutStopSec=30s", unit_text)
        self.assertIn("StartLimitIntervalSec=60s", unit_text)
        self.assertIn("StartLimitBurst=5", unit_text)
        self.assertIn("StartLimitAction=none", unit_text)
        self.assertIn("WatchdogSec=0", unit_text)
        self.assertIn("EnvironmentFile=", unit_text)
        self.assertIn("ExecStart=", unit_text)
        self.assertIn("--foreground", unit_text)
        self.assertIn("opencode.service", unit_text)

    def test_build_systemd_unit_includes_sandbox_directives(self):
        unit_text = _build_systemd_unit(Path("/home/DevCrewX/Projects/TelegramRemoteProgressBot"))

        self.assertIn("ProtectSystem=full", unit_text)
        self.assertIn("ProtectHome=true", unit_text)
        self.assertIn("PrivateTmp=true", unit_text)
        self.assertIn("PrivateDevices=true", unit_text)

    def test_build_opencode_systemd_unit_uses_serve(self):
        unit_text = _build_opencode_systemd_unit(Path("/home/DevCrewX/Projects/TelegramRemoteProgressBot"))

        self.assertIn("OpenCode API Server", unit_text)
        self.assertIn("opencode serve --hostname 127.0.0.1 --port 4096", unit_text)
        self.assertIn(f"EnvironmentFile={OPENCODE_CONFIG_FILE}", unit_text)

    def test_build_opencode_systemd_unit_includes_sandbox_directives(self):
        unit_text = _build_opencode_systemd_unit(Path("/home/DevCrewX/Projects/TelegramRemoteProgressBot"))

        self.assertIn("ProtectSystem=full", unit_text)
        self.assertIn("ProtectHome=true", unit_text)
        self.assertIn("PrivateTmp=true", unit_text)
        self.assertIn("PrivateDevices=true", unit_text)

    def test_render_systemd_command_prints_host_correct_units(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir)
            args = Mock(workspace=workspace_dir)

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                render_systemd_command(args)

            output = buffer.getvalue()
            self.assertIn("openbridge.service", output)
            self.assertIn("opencode.service", output)
            self.assertIn(str(workspace_dir.resolve()), output)
            self.assertFalse((workspace_dir / "openbridge.service").exists())
            self.assertFalse((workspace_dir / "opencode.service").exists())

            self.assertIn("Wants=opencode.service", output)
            self.assertIn("ExecStart=", output)

    def test_sync_opencode_env_contains_only_service_keys(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            bridge_env = Path(temp_dir) / "bridge.env"
            opencode_env = Path(temp_dir) / "opencode.env"
            write_env_file(
                bridge_env,
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENBRIDGE_INPUT_LLM_API_KEY": "sk-secret",
                    "OPENCODE_API_USERNAME": "opencode",
                    "OPENCODE_API_PASSWORD": "api-password",
                    "OPENCODE_SERVER_USERNAME": "server-user",
                    "OPENCODE_SERVER_PASSWORD": "server-password",
                },
            )

            with patch.object(app_module, "OPENCODE_CONFIG_FILE", opencode_env):
                _sync_opencode_env_from_bridge_config(bridge_env)

            loaded = read_env_file(opencode_env)
            self.assertEqual(loaded.get("OPENCODE_SERVER_USERNAME"), "server-user")
            self.assertEqual(loaded.get("OPENCODE_SERVER_PASSWORD"), "server-password")
            self.assertEqual(loaded.get("OPENCODE_API_USERNAME"), "opencode")
            self.assertEqual(loaded.get("OPENCODE_API_PASSWORD"), "api-password")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", loaded)
            self.assertNotIn("OPENBRIDGE_INPUT_LLM_API_KEY", loaded)

    def test_sync_opencode_env_falls_back_to_api_auth(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            bridge_env = Path(temp_dir) / "bridge.env"
            opencode_env = Path(temp_dir) / "opencode.env"
            write_env_file(
                bridge_env,
                {
                    "OPENCODE_API_USERNAME": "api-user",
                    "OPENCODE_API_PASSWORD": "api-pass",
                },
            )

            with patch.object(app_module, "OPENCODE_CONFIG_FILE", opencode_env):
                _sync_opencode_env_from_bridge_config(bridge_env)

            loaded = read_env_file(opencode_env)
            self.assertEqual(loaded.get("OPENCODE_SERVER_USERNAME"), "api-user")
            self.assertEqual(loaded.get("OPENCODE_SERVER_PASSWORD"), "api-pass")

    def test_version_flag_prints_release_version(self):
        parser = build_parser()

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), self.assertRaises(SystemExit) as exc:
            parser.parse_args(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertIn("openbridge 1.0.1", buffer.getvalue())

    def test_parser_includes_render_systemd_command(self):
        parser = build_parser()

        parsed = parser.parse_args(["render-systemd", "--workspace", "."])

        self.assertEqual(parsed.command, "render-systemd")
        self.assertEqual(parsed.func.__name__, "render_systemd_command")

    def test_parser_includes_deploy_validate_command(self):
        parser = build_parser()

        parsed = parser.parse_args(["deploy-validate", "--workspace", "."])

        self.assertEqual(parsed.command, "deploy-validate")
        self.assertEqual(parsed.func.__name__, "deploy_validate_command")

    def test_deploy_validate_command_reports_success(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir) / "workspace"
            workspace_dir.mkdir()
            config_path = Path(temp_dir) / "bridge.env"
            opencode_env = Path(temp_dir) / "opencode.env"
            write_env_file(
                config_path,
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": str(workspace_dir),
                    "OPENCODE_TIMEOUT_SECONDS": "600",
                    "OPENCODE_MAX_CONCURRENT": "1",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123456789",
                },
            )
            write_env_file(opencode_env, {"OPENCODE_SERVER_USERNAME": "opencode"})

            buffer = io.StringIO()
            with patch.object(app_module, "CONFIG_FILE", config_path), patch.object(
                app_module, "OPENCODE_CONFIG_FILE", opencode_env
            ), patch("builtins.print") as mock_print:
                deploy_validate_command(Mock(config=config_path, workspace=workspace_dir))

            printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
            self.assertIn("Deployment validation passed", printed)

    def test_deploy_validate_command_fails_on_missing_workspace(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_dir = Path(temp_dir) / "workspace"
            config_path = Path(temp_dir) / "bridge.env"
            opencode_env = Path(temp_dir) / "opencode.env"
            write_env_file(
                config_path,
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": str(workspace_dir),
                    "OPENCODE_TIMEOUT_SECONDS": "600",
                    "OPENCODE_MAX_CONCURRENT": "1",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123456789",
                },
            )
            write_env_file(opencode_env, {"OPENCODE_SERVER_USERNAME": "opencode"})

            with patch.object(app_module, "CONFIG_FILE", config_path), patch.object(
                app_module, "OPENCODE_CONFIG_FILE", opencode_env
            ), patch("builtins.print") as mock_print, self.assertRaises(SystemExit) as exc:
                deploy_validate_command(Mock(config=config_path, workspace=workspace_dir))

            self.assertEqual(exc.exception.code, 1)
            printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
            self.assertIn("OpenCode working dir does not exist", printed)

    def test_show_banner_prints_colored_ascii_art(self):
        from src.openbridge import app as app_module

        class TtyBuffer(io.StringIO):
            def isatty(self):
                return True

        expected_banner = (Path(__file__).resolve().parents[1] / "banner.txt").read_text(encoding="utf-8")
        buffer = TtyBuffer()
        with patch.object(app_module.sys, "stdout", buffer):
            _show_banner()

        output = buffer.getvalue()
        self.assertEqual(output, expected_banner)

    def test_load_banner_text_reads_banner_file(self):
        banner_text = _load_banner_text()

        self.assertIn("\x1b[38;2;255;153;102m", banner_text)
        self.assertGreater(len(banner_text), 1000)

    def test_get_resource_path_uses_bundle_root_when_present(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir)
            with patch.object(app_module.sys, "_MEIPASS", bundle_root, create=True):
                resource_path = get_resource_path("assets", "banner.txt")

        self.assertEqual(resource_path, bundle_root / "assets" / "banner.txt")

    def test_load_pid_removes_stale_pid_file(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "openbridge.pid"
            pid_file.write_text("999999\n", encoding="utf-8")

            with patch.object(app_module, "PID_FILE", pid_file), patch.object(
                app_module, "is_process_alive", return_value=False
            ):
                pid = _load_pid()

        self.assertIsNone(pid)
        self.assertFalse(pid_file.exists())

    def test_is_process_alive_handles_invalid_pid(self):
        self.assertFalse(is_process_alive(-1))

    def test_setup_writes_opencode_server_auth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            data = {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "OPENCODE_API_BASE_URL": "http://127.0.0.1:4096",
                "OPENCODE_API_USERNAME": "opencode",
                "OPENCODE_API_PASSWORD": "pw",
                "OPENCODE_API_TIMEOUT_SECONDS": "150",
                "OPENCODE_SERVER_USERNAME": "server-user",
                "OPENCODE_SERVER_PASSWORD": "server-pw",
            }

            write_env_file(config_path, data)
            loaded = read_env_file(config_path)

            self.assertEqual(loaded["OPENCODE_SERVER_USERNAME"], "server-user")
            self.assertEqual(loaded["OPENCODE_SERVER_PASSWORD"], "server-pw")

    def test_merged_config_reads_secret_from_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            secret_file = Path(temp_dir) / "token.secret"
            secret_file.write_text("123:file-token\n", encoding="utf-8")

            write_env_file(
                config_path,
                {
                    "TELEGRAM_BOT_TOKEN_FILE": str(secret_file),
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "600",
                    "OPENCODE_MAX_CONCURRENT": "1",
                },
            )

            config = _merged_config(config_path)

            self.assertEqual(config.telegram_token, "123:file-token")

    def test_merged_config_uses_env_secret_when_not_in_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            write_env_file(
                config_path,
                {
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "600",
                    "OPENCODE_MAX_CONCURRENT": "1",
                },
            )

            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:env-token"}, clear=False):
                config = _merged_config(config_path)

            self.assertEqual(config.telegram_token, "123:env-token")

    def test_merged_config_maps_legacy_secret_file_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            secret_file = Path(temp_dir) / "legacy-input-key.secret"
            secret_file.write_text("legacy-secret-key\n", encoding="utf-8")

            config_path.write_text(
                "\n".join(
                    [
                        'export TELEGRAM_BOT_TOKEN="123:token"',
                        'export OPENCODE_MODEL="opencode/big-pickle"',
                        f'export OPENCODE_WORKING_DIR="{temp_dir}"',
                        'export OPENCODE_TIMEOUT_SECONDS="600"',
                        'export OPENCODE_MAX_CONCURRENT="1"',
                        'export TELEWATCH_INPUT_LLM_ENABLED="1"',
                        'export TELEWATCH_INPUT_LLM_PROVIDER="api"',
                        'export TELEWATCH_INPUT_LLM_MODEL="legacy-model"',
                        'export TELEWATCH_INPUT_LLM_BASE_URL="https://example.test/v1"',
                        f'export TELEWATCH_INPUT_LLM_API_KEY_FILE="{secret_file}"',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            config = _merged_config(config_path)

            self.assertTrue(config.input_llm_enabled)
            self.assertEqual(config.input_llm_provider, "api")
            self.assertEqual(config.input_llm_api_key, "legacy-secret-key")

    def test_uninstall_systemd_command_removes_unit(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            unit_file = Path(temp_dir) / "openbridge.service"
            opencode_unit_file = Path(temp_dir) / "opencode.service"
            unit_file.write_text("[Unit]\n", encoding="utf-8")
            opencode_unit_file.write_text("[Unit]\n", encoding="utf-8")

            with patch.object(app_module, "SYSTEMD_UNIT_FILE", unit_file), patch.object(
                app_module, "OPENCODE_SYSTEMD_UNIT_FILE", opencode_unit_file
            ), patch.object(app_module, "SYSTEMD_UNIT_NAME", "openbridge.service"), patch.object(
                app_module.shutil, "which", return_value="/bin/systemctl"
            ), patch.object(app_module.subprocess, "run") as mock_run:
                app_module.uninstall_systemd_command(Mock())

                self.assertFalse(unit_file.exists())
                self.assertFalse(opencode_unit_file.exists())
                self.assertEqual(
                    [call.args[0] for call in mock_run.call_args_list],
                    [
                        ["systemctl", "--user", "disable", "openbridge.service"],
                        ["systemctl", "--user", "daemon-reload"],
                    ],
                )

    def test_start_command_does_not_manage_opencode_lifecycle(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "bridge.env"
            write_env_file(
                config_path,
                {
                    "TELEGRAM_BOT_TOKEN": "123:token",
                    "OPENCODE_MODEL": "opencode/big-pickle",
                    "OPENCODE_WORKING_DIR": temp_dir,
                    "OPENCODE_TIMEOUT_SECONDS": "600",
                    "OPENCODE_MAX_CONCURRENT": "1",
                    "OPENCODE_API_BASE_URL": "http://127.0.0.1:4096",
                    "OPENCODE_API_USERNAME": "opencode",
                    "OPENCODE_API_PASSWORD": "pw",
                    "OPENCODE_API_TIMEOUT_SECONDS": "150",
                },
            )

            with patch.object(app_module.shutil, "which", side_effect=lambda cmd: "/bin/systemctl" if cmd == "systemctl" else None), patch.object(
                app_module, "run_bridge"
            ) as mock_run_bridge:
                app_module.start_command(Mock(config=config_path, foreground=True, debug=False, log_level=None))

                mock_run_bridge.assert_called_once()

    def test_workflows_init_warns_on_placeholder_targets(self):
        from src.openbridge import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            workflows_file = Path(temp_dir) / "workflows.json"
            args = Mock(workflows_file=workflows_file, force=False)

            with patch.object(app_module, "WORKFLOWS_FILE", workflows_file), patch("builtins.print") as mock_print:
                workflows_init_command(args)

            self.assertTrue(workflows_file.exists())
            printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
            self.assertIn("placeholder Telegram targets", printed)
            self.assertIn("daily_news_digest", printed)

    def test_stop_command_stops_systemd_service_when_pid_missing(self):
        from src.openbridge import app as app_module

        with patch.object(app_module, "_load_pid", return_value=None), patch.object(
            app_module.shutil, "which", return_value="/bin/systemctl"
        ), patch.object(app_module.subprocess, "run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0),  # systemctl stop
                Mock(returncode=0, stdout=""),  # ps scan
            ]

            app_module.stop_command(Mock())

            commands = [call.args[0] for call in mock_run.call_args_list]
            self.assertIn(["systemctl", "--user", "stop", "openbridge.service"], commands)
            self.assertIn(["ps", "-eo", "pid=,args="], commands)

    def test_stop_command_force_terminates_foreground_process(self):
        from src.openbridge import app as app_module

        with patch.object(app_module, "_load_pid", return_value=None), patch.object(
            app_module.shutil, "which"
        ) as mock_which, patch.object(app_module.subprocess, "run") as mock_run:
            def which_side_effect(cmd):
                if cmd == "systemctl":
                    return "/bin/systemctl"
                return None

            mock_which.side_effect = which_side_effect
            mock_run.side_effect = [
                Mock(returncode=0),  # systemctl stop
                Mock(returncode=0, stdout=""),  # ps scan
            ]

            args = Mock(force=True)
            app_module.stop_command(args)

            commands = [call.args[0] for call in mock_run.call_args_list]
            self.assertIn(["systemctl", "--user", "stop", "openbridge.service"], commands)
            self.assertIn(["ps", "-eo", "pid=,args="], commands)

    def test_missing_dependencies_detects_absent_binaries(self):
        def which_side_effect(binary):
            if binary in {"npm", "npx"}:
                return f"/usr/bin/{binary}"
            return None

        with patch("src.openbridge.app.shutil.which", side_effect=which_side_effect):
            missing = _missing_dependencies()

        self.assertIn("opencode", missing)
        self.assertIn("@googleworkspace/cli", missing)
        self.assertIn("gws-mcp-server", missing)
        self.assertNotIn("npm", missing)
        self.assertNotIn("npx", missing)

    def test_install_missing_dependencies_runs_npm_installs_when_approved(self):
        missing = {
            "@googleworkspace/cli": {
                "binary": "gws",
                "install_commands": [["npm", "install", "-g", "@googleworkspace/cli"]],
                "manual_hint": "manual",
            },
            "gws-mcp-server": {
                "binary": "gws-mcp-server",
                "install_commands": [["npm", "install", "-g", "gws-mcp-server"]],
                "manual_hint": "manual",
            },
        }

        def which_side_effect(binary):
            if binary == "npm":
                return "/usr/bin/npm"
            return None

        with patch("src.openbridge.app._prompt", return_value="y"), patch(
            "src.openbridge.app.shutil.which", side_effect=which_side_effect
        ), patch("src.openbridge.app.subprocess.run") as mock_run, patch("builtins.print"):
            _install_missing_dependencies(missing)

        self.assertEqual(
            [call.args[0] for call in mock_run.call_args_list],
            [
                ["npm", "install", "-g", "@googleworkspace/cli"],
                ["npm", "install", "-g", "gws-mcp-server"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
