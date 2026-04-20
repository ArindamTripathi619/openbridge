from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from .opencode_bridge import BridgeConfig, run_bridge

APP_DIR = Path.home() / ".config" / "telewatch"
CONFIG_FILE = APP_DIR / "bridge.env"
LOG_FILE = APP_DIR / "telewatch.log"
PID_FILE = APP_DIR / "telewatch.pid"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_UNIT_NAME = "telewatch.service"
SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / SYSTEMD_UNIT_NAME

CONFIG_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "OPENCODE_MODEL",
    "OPENCODE_WORKING_DIR",
    "OPENCODE_TIMEOUT_SECONDS",
    "OPENCODE_MAX_CONCURRENT",
    "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
    "TELEWATCH_GWS_TIMEOUT_SECONDS",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "LOG_LEVEL",
    "TELEWATCH_INPUT_LLM_ENABLED",
    "TELEWATCH_INPUT_LLM_PROVIDER",
    "TELEWATCH_INPUT_LLM_API_KEY",
    "TELEWATCH_INPUT_LLM_MODEL",
    "TELEWATCH_INPUT_LLM_BASE_URL",
    "TELEWATCH_INPUT_LLM_LITELLM_PORT",
    "TELEWATCH_INPUT_LLM_TIMEOUT_SECONDS",
    "TELEWATCH_OUTPUT_LLM_ENABLED",
    "TELEWATCH_OUTPUT_LLM_PROVIDER",
    "TELEWATCH_OUTPUT_LLM_API_KEY",
    "TELEWATCH_OUTPUT_LLM_MODEL",
    "TELEWATCH_OUTPUT_LLM_BASE_URL",
    "TELEWATCH_OUTPUT_LLM_LITELLM_PORT",
    "TELEWATCH_OUTPUT_LLM_TIMEOUT_SECONDS",
    "TELEWATCH_DECORATOR_ENABLED",
    "TELEWATCH_DECORATOR_API_KEY",
    "TELEWATCH_DECORATOR_MODEL",
    "TELEWATCH_DECORATOR_BASE_URL",
    "TELEWATCH_DECORATOR_TIMEOUT_SECONDS",
]


def _prompt(
    message: str,
    default: Optional[str] = None,
    *,
    secret: bool = False,
    display_default: Optional[str] = None,
) -> str:
    visible_default = display_default if display_default is not None else default
    suffix = f" [{visible_default}]" if visible_default else ""
    prompt_text = f"{message}{suffix}: "
    try:
        if secret:
            import getpass

            value = getpass.getpass(prompt_text)
        else:
            value = input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        raise SystemExit(1)

    value = value.strip()
    if not value and default is not None:
        return default
    return value


def _format_env_value(value: str) -> str:
    return json.dumps(value)


def read_env_file(path: Path) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not path.exists():
        return data

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key] = value
    return data


def write_env_file(path: Path, data: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# TeleWatch bridge configuration"]
    for key in CONFIG_KEYS:
        value = data.get(key, "")
        if value:
            lines.append(f"export {key}={_format_env_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _merged_config(config_path: Path, overrides: Optional[Dict[str, str]] = None) -> BridgeConfig:
    data = read_env_file(config_path)
    data.update(overrides or {})
    return BridgeConfig.from_mapping(data)


def _daemonize(log_file: Path) -> None:
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.setsid()

    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    os.chdir("/")
    os.umask(0)

    with open(os.devnull, "rb", buffering=0) as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a+", buffering=1) as log_handle:
        os.dup2(log_handle.fileno(), sys.stdout.fileno())
        os.dup2(log_handle.fileno(), sys.stderr.fileno())


def _write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid() -> None:
    if PID_FILE.exists():
        PID_FILE.unlink()


def _load_pid() -> Optional[int]:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _build_systemd_unit(workspace_dir: Path) -> str:
    telewatch_executable = Path(sys.executable).resolve().parent / "telewatch"
    exec_start = str(telewatch_executable)
    if not telewatch_executable.exists():
        exec_start = f"{sys.executable} -m telewatch.app start --foreground"

    return (
        "[Unit]\n"
        "Description=TeleWatch Telegram OpenCode Bridge\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workspace_dir}\n"
        f"EnvironmentFile={CONFIG_FILE}\n"
        f"ExecStart={exec_start} --foreground\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=5\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_systemd_command(args: argparse.Namespace) -> None:
    workspace_dir = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()

    if not CONFIG_FILE.exists():
        print(f"Config not found: {CONFIG_FILE}")
        print("Run: telewatch setup")
        raise SystemExit(1)

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT_FILE.write_text(_build_systemd_unit(workspace_dir), encoding="utf-8")

    print(f"Installed systemd unit to {SYSTEMD_UNIT_FILE}")

    if shutil.which("systemctl") is None:
        print("systemctl not found; reload and enable the unit manually if needed.")
        return

    commands = [["systemctl", "--user", "daemon-reload"]]
    if not getattr(args, "no_enable", False):
        commands.append(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME])
    if getattr(args, "start", False):
        commands.append(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME])

    for command in commands:
        subprocess.run(command, check=True)

    if getattr(args, "start", False):
        print(f"Enabled and restarted {SYSTEMD_UNIT_NAME}")
    elif not getattr(args, "no_enable", False):
        print(f"Enabled {SYSTEMD_UNIT_NAME}")
    else:
        print(f"Reloaded user systemd; {SYSTEMD_UNIT_NAME} was not enabled")


def uninstall_systemd_command(_: argparse.Namespace) -> None:
    unit_exists = SYSTEMD_UNIT_FILE.exists()
    if shutil.which("systemctl") is not None:
        subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], check=False)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    if unit_exists:
        SYSTEMD_UNIT_FILE.unlink()
        print(f"Removed {SYSTEMD_UNIT_FILE}")
    else:
        print(f"No systemd unit found at {SYSTEMD_UNIT_FILE}")

    if shutil.which("systemctl") is None:
        print("systemctl not found; remove the unit manually if needed.")
    else:
        print(f"Disabled {SYSTEMD_UNIT_NAME} and reloaded user systemd")


def setup_command(_: argparse.Namespace) -> None:
    print("Telegram OpenCode Bridge Setup")
    print("================================")

    current = read_env_file(CONFIG_FILE)
    config: Dict[str, str] = {}

    config["TELEGRAM_BOT_TOKEN"] = _prompt(
        "Telegram bot token",
        current.get("TELEGRAM_BOT_TOKEN"),
        secret=True,
        display_default="<hidden>",
    )
    config["OPENCODE_MODEL"] = _prompt("OpenCode model", current.get("OPENCODE_MODEL", "opencode/big-pickle"))
    config["OPENCODE_WORKING_DIR"] = _prompt(
        "OpenCode working dir",
        current.get("OPENCODE_WORKING_DIR", str(Path.cwd())),
    )
    config["OPENCODE_TIMEOUT_SECONDS"] = _prompt(
        "Timeout seconds",
        current.get("OPENCODE_TIMEOUT_SECONDS", "600"),
    )
    config["OPENCODE_MAX_CONCURRENT"] = _prompt(
        "Max concurrent jobs",
        current.get("OPENCODE_MAX_CONCURRENT", "1"),
    )
    config["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = _prompt(
        "Google Workspace credentials file (optional for gws)",
        current.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", ""),
        display_default="<set>" if current.get("GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE", "").strip() else "",
    )
    config["TELEWATCH_GWS_TIMEOUT_SECONDS"] = _prompt(
        "gws timeout seconds",
        current.get("TELEWATCH_GWS_TIMEOUT_SECONDS", "120"),
    )
    config["LOG_LEVEL"] = _prompt("Log level", current.get("LOG_LEVEL", "INFO"))

    def _chat_ids_default(value: str) -> str:
        if not value.strip():
            return ""
        count = len([item for item in value.split(",") if item.strip()])
        return f"<set:{count} id(s)>"

    config["TELEGRAM_ALLOWED_CHAT_IDS"] = _prompt(
        "Allowed chat ids (comma-separated, blank = allow all)",
        current.get("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        display_default=_chat_ids_default(current.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
    )

    def configure_llm_role(prefix: str, label: str) -> None:
        enabled_default = "Y" if current.get(f"{prefix}_ENABLED", "0") in {"1", "true", "yes", "on"} else "N"
        enable = _prompt(f"Enable {label}? [y/N]", enabled_default).lower()
        if enable not in {"y", "yes"}:
            config[f"{prefix}_ENABLED"] = "0"
            config[f"{prefix}_PROVIDER"] = "none"
            return

        config[f"{prefix}_ENABLED"] = "1"
        provider_default = current.get(f"{prefix}_PROVIDER", "litellm") or "litellm"
        provider = _prompt(
            f"{label} provider [litellm/api]",
            provider_default,
        ).strip().lower()
        if provider not in {"litellm", "api"}:
            provider = "litellm"

        config[f"{prefix}_PROVIDER"] = provider

        if provider == "litellm":
            config[f"{prefix}_MODEL"] = _prompt(
                f"{label} model (LiteLLM)",
                current.get(f"{prefix}_MODEL", "groq-gpt-oss-mini"),
            )
            config[f"{prefix}_LITELLM_PORT"] = _prompt(
                f"{label} LiteLLM port",
                current.get(f"{prefix}_LITELLM_PORT", "8000"),
            )
            config[f"{prefix}_TIMEOUT_SECONDS"] = _prompt(
                f"{label} timeout seconds",
                current.get(f"{prefix}_TIMEOUT_SECONDS", "30"),
            )
            config[f"{prefix}_API_KEY"] = current.get(f"{prefix}_API_KEY", "") or "sk-local"
            config[f"{prefix}_BASE_URL"] = ""
        else:
            config[f"{prefix}_API_KEY"] = _prompt(
                f"{label} API key",
                current.get(f"{prefix}_API_KEY", ""),
                secret=True,
                display_default="<hidden>",
            )
            config[f"{prefix}_MODEL"] = _prompt(
                f"{label} model",
                current.get(f"{prefix}_MODEL", ""),
            )
            config[f"{prefix}_BASE_URL"] = _prompt(
                f"{label} base URL",
                current.get(f"{prefix}_BASE_URL", ""),
            )
            config[f"{prefix}_TIMEOUT_SECONDS"] = _prompt(
                f"{label} timeout seconds",
                current.get(f"{prefix}_TIMEOUT_SECONDS", "30"),
            )
            config[f"{prefix}_LITELLM_PORT"] = current.get(f"{prefix}_LITELLM_PORT", "8000")

    configure_llm_role("TELEWATCH_INPUT_LLM", "input prompt enhancer")
    configure_llm_role("TELEWATCH_OUTPUT_LLM", "output prettifier")

    # Keep legacy decorator keys for backwards compatibility, but default them off in new setups.
    config["TELEWATCH_DECORATOR_ENABLED"] = "0"
    config["TELEWATCH_DECORATOR_API_KEY"] = ""
    config["TELEWATCH_DECORATOR_MODEL"] = ""
    config["TELEWATCH_DECORATOR_BASE_URL"] = ""
    config["TELEWATCH_DECORATOR_TIMEOUT_SECONDS"] = "30"

    write_env_file(CONFIG_FILE, config)
    print(f"Saved configuration to {CONFIG_FILE}")

    start_now = _prompt("Start the app now? [Y/n]", "Y").lower()
    if start_now in {"", "y", "yes"}:
        start_command(argparse.Namespace(config=CONFIG_FILE, foreground=False, debug=False, log_level=None))


def start_command(args: argparse.Namespace) -> None:
    config_path = Path(args.config) if args.config else CONFIG_FILE
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run: telewatch setup")
        raise SystemExit(1)

    overrides: Dict[str, str] = {}
    if getattr(args, "debug", False):
        overrides["LOG_LEVEL"] = "DEBUG"
    if getattr(args, "log_level", None):
        overrides["LOG_LEVEL"] = str(args.log_level)

    config = _merged_config(config_path, overrides)

    if not Path(config.opencode_working_dir).exists():
        print(f"OpenCode working dir does not exist: {config.opencode_working_dir}")
        raise SystemExit(1)

    if not getattr(args, "foreground", False):
        _daemonize(LOG_FILE)
        _write_pid()
        try:
            run_bridge(config, foreground=False, log_file=LOG_FILE)
        finally:
            _remove_pid()
    else:
        run_bridge(config, foreground=True, log_file=LOG_FILE)


def stop_command(_: argparse.Namespace) -> None:
    pid = _load_pid()
    if not pid:
        print("No running background process found.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        print("Process not found; cleaning stale pid file.")
    finally:
        _remove_pid()


def status_command(_: argparse.Namespace) -> None:
    pid = _load_pid()
    if pid:
        print(f"Running in background with PID {pid}")
        return

    print("Not running.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telewatch", description="Telegram OpenCode Bridge")
    subparsers = parser.add_subparsers(dest="command")

    setup_parser = subparsers.add_parser("setup", help="Run the setup wizard")
    setup_parser.set_defaults(func=setup_command)

    start_parser = subparsers.add_parser("start", help="Start the bridge")
    start_parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Path to config env file")
    start_parser.add_argument("--foreground", action="store_true", help="Run in the foreground for debugging")
    start_parser.add_argument("--debug", action="store_true", help="Foreground mode with DEBUG logging")
    start_parser.add_argument("--log-level", default=None, help="Override log level")
    start_parser.set_defaults(func=start_command)

    stop_parser = subparsers.add_parser("stop", help="Stop the background bridge")
    stop_parser.set_defaults(func=stop_command)

    status_parser = subparsers.add_parser("status", help="Show whether the bridge is running")
    status_parser.set_defaults(func=status_command)

    install_systemd_parser = subparsers.add_parser("install-systemd", help="Install the user systemd unit")
    install_systemd_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory to run the bridge from",
    )
    install_systemd_parser.add_argument(
        "--no-enable",
        action="store_true",
        help="Write the unit and reload systemd without enabling it",
    )
    install_systemd_parser.add_argument(
        "--start",
        action="store_true",
        help="Restart the service after installing it",
    )
    install_systemd_parser.set_defaults(func=install_systemd_command)

    uninstall_systemd_parser = subparsers.add_parser("uninstall-systemd", help="Remove the user systemd unit")
    uninstall_systemd_parser.set_defaults(func=uninstall_systemd_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        raise SystemExit(1)

    args.func(args)


if __name__ == "__main__":
    main()
