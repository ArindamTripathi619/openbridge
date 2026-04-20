import tempfile
import unittest
from pathlib import Path
import os
import sys
from unittest.mock import patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.telewatch.app import _build_systemd_unit, read_env_file, write_env_file
from src.telewatch.opencode_bridge import BridgeConfig


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
                "TELEGRAM_ALLOWED_CHAT_IDS": "123,456",
                "LOG_LEVEL": "DEBUG",
            }

            write_env_file(config_path, original)
            loaded = read_env_file(config_path)

            self.assertEqual(loaded["TELEGRAM_BOT_TOKEN"], original["TELEGRAM_BOT_TOKEN"])
            self.assertEqual(loaded["OPENCODE_MODEL"], original["OPENCODE_MODEL"])
            self.assertEqual(loaded["TELEGRAM_ALLOWED_CHAT_IDS"], original["TELEGRAM_ALLOWED_CHAT_IDS"])
            self.assertEqual(loaded["LOG_LEVEL"], original["LOG_LEVEL"])

    def test_bridge_config_from_mapping(self):
        config = BridgeConfig.from_mapping(
            {
                "TELEGRAM_BOT_TOKEN": "123:token",
                "OPENCODE_MODEL": "opencode/big-pickle",
                "OPENCODE_WORKING_DIR": "/tmp/project",
                "OPENCODE_TIMEOUT_SECONDS": "600",
                "OPENCODE_MAX_CONCURRENT": "1",
                "TELEGRAM_ALLOWED_CHAT_IDS": "123,456",
                "LOG_LEVEL": "info",
            }
        )

        self.assertEqual(config.telegram_token, "123:token")
        self.assertEqual(config.opencode_model, "opencode/big-pickle")
        self.assertEqual(config.opencode_working_dir, "/tmp/project")
        self.assertEqual(config.opencode_timeout_seconds, 600)
        self.assertEqual(config.max_concurrent_jobs, 1)
        self.assertEqual(config.allowed_chat_ids, {123, 456})
        self.assertEqual(config.log_level, "INFO")

    def test_build_systemd_unit_includes_restart_policy(self):
        unit_text = _build_systemd_unit(Path("/home/DevCrewX/Projects/TelegramRemoteProgressBot"))

        self.assertIn("Restart=on-failure", unit_text)
        self.assertIn("RestartSec=5", unit_text)
        self.assertIn("EnvironmentFile=", unit_text)
        self.assertIn("ExecStart=", unit_text)
        self.assertIn("--foreground", unit_text)

    def test_uninstall_systemd_command_removes_unit(self):
        from src.telewatch import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            unit_file = Path(temp_dir) / "telewatch.service"
            unit_file.write_text("[Unit]\n", encoding="utf-8")

            with patch.object(app_module, "SYSTEMD_UNIT_FILE", unit_file), patch.object(
                app_module, "SYSTEMD_UNIT_NAME", "telewatch.service"
            ), patch.object(app_module.shutil, "which", return_value="/bin/systemctl"), patch.object(
                app_module.subprocess, "run"
            ) as mock_run:
                app_module.uninstall_systemd_command(unittest.mock.Mock())

                self.assertFalse(unit_file.exists())
                self.assertEqual(
                    [call.args[0] for call in mock_run.call_args_list],
                    [
                        ["systemctl", "--user", "disable", "telewatch.service"],
                        ["systemctl", "--user", "daemon-reload"],
                    ],
                )


if __name__ == "__main__":
    unittest.main()
