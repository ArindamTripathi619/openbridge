from __future__ import annotations

import errno
import argparse
import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import __version__
from .opencode_bridge import BridgeConfig, run_bridge
from .workflows import (
    DEFAULT_WORKFLOWS_FILE,
    DEFAULT_WORKFLOWS_STATE_FILE,
    WorkflowManager,
    WorkflowStateStore,
    create_manager,
    load_workflows,
    _format_timestamp,
    _next_run_timestamp,
    sample_workflows,
    save_workflows,
)

APP_DIR = Path.home() / ".config" / "openbridge"
CONFIG_FILE = APP_DIR / "bridge.env"
OPENCODE_CONFIG_FILE = APP_DIR / "opencode.env"
LOG_FILE = APP_DIR / "openbridge.log"
PID_FILE = APP_DIR / "openbridge.pid"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
SYSTEMD_UNIT_NAME = "openbridge.service"
SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / SYSTEMD_UNIT_NAME
OPENCODE_SYSTEMD_UNIT_NAME = "opencode.service"
OPENCODE_SYSTEMD_UNIT_FILE = SYSTEMD_USER_DIR / OPENCODE_SYSTEMD_UNIT_NAME
SYSTEMD_TIMEOUT_START_SEC = "2min"
SYSTEMD_TIMEOUT_STOP_SEC = "30s"
SYSTEMD_RESTART_SEC = "5s"
SYSTEMD_START_LIMIT_INTERVAL_SEC = "60s"
SYSTEMD_START_LIMIT_BURST = 5
SYSTEMD_START_LIMIT_ACTION = "none"
SYSTEMD_WATCHDOG_SEC = "0"
WORKFLOWS_FILE = DEFAULT_WORKFLOWS_FILE
WORKFLOWS_STATE_FILE = DEFAULT_WORKFLOWS_STATE_FILE
LEGACY_ENV_PREFIX = "TELEWATCH_"
CURRENT_ENV_PREFIX = "OPENBRIDGE_"

REQUIRED_DEPENDENCIES = {
    "npm": {
        "binary": "npm",
        "install_commands": [],
        "manual_hint": "Install Node.js/npm from your OS package manager or nodejs.org.",
    },
    "npx": {
        "binary": "npx",
        "install_commands": [],
        "manual_hint": "Install Node.js/npm from your OS package manager or nodejs.org.",
    },
    "opencode": {
        "binary": "opencode",
        "install_commands": [["npm", "install", "-g", "opencode"]],
        "manual_hint": "Install OpenCode CLI manually if your environment uses a different package name.",
    },
    "@googleworkspace/cli": {
        "binary": "gws",
        "install_commands": [["npm", "install", "-g", "@googleworkspace/cli"]],
        "manual_hint": "Install the Google Workspace CLI manually and ensure `gws` is on PATH.",
    },
    "gws-mcp-server": {
        "binary": "gws-mcp-server",
        "install_commands": [["npm", "install", "-g", "gws-mcp-server"]],
        "manual_hint": "Install gws-mcp-server manually and ensure it is on PATH.",
    },
}

CONFIG_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "OPENCODE_MODEL",
    "OPENCODE_WORKING_DIR",
    "OPENCODE_TIMEOUT_SECONDS",
    "OPENCODE_MAX_CONCURRENT",
    "OPENCODE_API_BASE_URL",
    "OPENCODE_API_USERNAME",
    "OPENCODE_API_PASSWORD",
    "OPENCODE_API_TIMEOUT_SECONDS",
    "OPENBRIDGE_CHAT_QUEUE_MAX_PENDING",
    "OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE",
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_ALLOW_ALL_CHATS",
    "LOG_LEVEL",
    "OPENBRIDGE_INPUT_LLM_ENABLED",
    "OPENBRIDGE_INPUT_LLM_PROVIDER",
    "OPENBRIDGE_INPUT_LLM_API_KEY",
    "OPENBRIDGE_INPUT_LLM_MODEL",
    "OPENBRIDGE_INPUT_LLM_BASE_URL",
    "OPENBRIDGE_INPUT_LLM_LITELLM_PORT",
    "OPENBRIDGE_INPUT_LLM_TIMEOUT_SECONDS",
    "OPENBRIDGE_OUTPUT_LLM_ENABLED",
    "OPENBRIDGE_OUTPUT_LLM_PROVIDER",
    "OPENBRIDGE_OUTPUT_LLM_API_KEY",
    "OPENBRIDGE_OUTPUT_LLM_MODEL",
    "OPENBRIDGE_OUTPUT_LLM_BASE_URL",
    "OPENBRIDGE_OUTPUT_LLM_LITELLM_PORT",
    "OPENBRIDGE_OUTPUT_LLM_TIMEOUT_SECONDS",
    "OPENBRIDGE_DECORATOR_ENABLED",
    "OPENBRIDGE_DECORATOR_API_KEY",
    "OPENBRIDGE_DECORATOR_MODEL",
    "OPENBRIDGE_DECORATOR_BASE_URL",
    "OPENBRIDGE_DECORATOR_TIMEOUT_SECONDS",
    "TELEGRAM_BOT_TOKEN_FILE",
    "OPENCODE_API_PASSWORD_FILE",
    "OPENCODE_SERVER_PASSWORD_FILE",
    "OPENBRIDGE_INPUT_LLM_API_KEY_FILE",
    "OPENBRIDGE_OUTPUT_LLM_API_KEY_FILE",
    "OPENBRIDGE_DECORATOR_API_KEY_FILE",
]

SENSITIVE_CONFIG_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OPENCODE_API_PASSWORD",
    "OPENCODE_SERVER_PASSWORD",
    "OPENBRIDGE_INPUT_LLM_API_KEY",
    "OPENBRIDGE_OUTPUT_LLM_API_KEY",
    "OPENBRIDGE_DECORATOR_API_KEY",
)

OPENCODE_SERVICE_CONFIG_KEYS = [
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_SERVER_PASSWORD_FILE",
    "OPENCODE_API_USERNAME",
    "OPENCODE_API_PASSWORD",
    "OPENCODE_API_PASSWORD_FILE",
]


def _read_secret_from_file(path_value: str) -> str:
    secret_path = Path(path_value).expanduser()
    if not secret_path.exists():
        raise FileNotFoundError(f"Secret file does not exist: {secret_path}")
    if not secret_path.is_file():
        raise ValueError(f"Secret path is not a file: {secret_path}")
    return secret_path.read_text(encoding="utf-8").strip()


def _hydrate_sensitive_values(data: Dict[str, str]) -> Dict[str, str]:
    hydrated = dict(data)
    for key in SENSITIVE_CONFIG_KEYS:
        existing = hydrated.get(key, "").strip()
        if existing:
            continue

        file_key = f"{key}_FILE"
        file_value = hydrated.get(file_key, "").strip() or os.environ.get(file_key, "").strip()
        if file_value:
            try:
                hydrated[key] = _read_secret_from_file(file_value)
            except Exception as exc:
                raise ValueError(f"Could not read {file_key}: {exc}") from exc
            continue

        env_value = os.environ.get(key, "").strip()
        if env_value:
            hydrated[key] = env_value
    return hydrated


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
    os.chmod(path.parent, 0o700)
    lines = ["# OpenBridge bridge configuration"]
    for key in CONFIG_KEYS:
        value = data.get(key, "")
        if value:
            lines.append(f"export {key}={_format_env_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _write_opencode_env_file(path: Path, data: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    lines = ["# OpenCode service configuration"]
    for key in OPENCODE_SERVICE_CONFIG_KEYS:
        value = data.get(key, "")
        if value:
            lines.append(f"export {key}={_format_env_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _sync_opencode_env_from_bridge_config(config_path: Path = CONFIG_FILE) -> None:
    bridge_data = _with_legacy_openbridge_aliases(read_env_file(config_path))
    opencode_data: Dict[str, str] = {}

    for key in OPENCODE_SERVICE_CONFIG_KEYS:
        value = bridge_data.get(key, "").strip()
        if value:
            opencode_data[key] = value

    # Backward compatibility: if server auth values are missing, reuse API auth.
    if "OPENCODE_SERVER_USERNAME" not in opencode_data:
        fallback = bridge_data.get("OPENCODE_API_USERNAME", "").strip()
        if fallback:
            opencode_data["OPENCODE_SERVER_USERNAME"] = fallback
    if "OPENCODE_SERVER_PASSWORD" not in opencode_data and "OPENCODE_SERVER_PASSWORD_FILE" not in opencode_data:
        fallback_secret = bridge_data.get("OPENCODE_API_PASSWORD", "").strip()
        fallback_secret_file = bridge_data.get("OPENCODE_API_PASSWORD_FILE", "").strip()
        if fallback_secret:
            opencode_data["OPENCODE_SERVER_PASSWORD"] = fallback_secret
        elif fallback_secret_file:
            opencode_data["OPENCODE_SERVER_PASSWORD_FILE"] = fallback_secret_file

    _write_opencode_env_file(OPENCODE_CONFIG_FILE, opencode_data)


def _with_legacy_openbridge_aliases(data: Dict[str, str]) -> Dict[str, str]:
    normalized = dict(data)
    for key, value in data.items():
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue

        suffix = key[len(LEGACY_ENV_PREFIX) :]
        current_key = f"{CURRENT_ENV_PREFIX}{suffix}"
        current_value = str(normalized.get(current_key, "")).strip()
        if current_value:
            continue

        normalized[current_key] = value
    return normalized


def get_resource_path(*parts: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidate = bundle_root.joinpath(*parts)
    if candidate.exists():
        return candidate

    project_root = Path(__file__).resolve().parents[2]
    fallback = project_root.joinpath(*parts)
    if fallback.exists():
        return fallback

    return candidate


def _load_banner_text() -> str:
    banner_path = get_resource_path("banner.txt")
    if banner_path.exists():
        return banner_path.read_text(encoding="utf-8")
    return ""


def is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def _install_signal_handlers(stop_event: threading.Event) -> Dict[int, Any]:
    previous_handlers: Dict[int, Any] = {}

    def _handle_signal(_signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, _handle_signal)

    return previous_handlers


def _restore_signal_handlers(previous_handlers: Dict[int, Any]) -> None:
    for signum, handler in previous_handlers.items():
        try:
            signal.signal(signum, handler)
        except Exception:
            pass


def _merged_config(config_path: Path, overrides: Optional[Dict[str, str]] = None) -> BridgeConfig:
    data = read_env_file(config_path)
    data.update(overrides or {})
    data = _with_legacy_openbridge_aliases(data)
    data = _hydrate_sensitive_values(data)
    return BridgeConfig.from_mapping(data)


def _daemonize(log_file: Path) -> Optional[int]:
    pid = os.fork()
    if pid > 0:
        return pid

    os.setsid()

    os.chdir("/")
    os.umask(0)

    with open(os.devnull, "rb", buffering=0) as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a+", buffering=1) as log_handle:
        os.dup2(log_handle.fileno(), sys.stdout.fileno())
        os.dup2(log_handle.fileno(), sys.stderr.fileno())

    return None


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
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        _remove_pid()
        return None

    if not is_process_alive(pid):
        _remove_pid()
        return None

    return pid


def _build_systemd_unit(workspace_dir: Path) -> str:
    openbridge_executable = Path(sys.executable).resolve().parent / "openbridge"
    exec_start = str(openbridge_executable)
    if not openbridge_executable.exists():
        exec_start = f"{sys.executable} -m openbridge.app start --foreground"

    return (
        "[Unit]\n"
        "Description=OpenBridge Telegram OpenCode Bridge\n"
        "Wants=network-online.target\n"
        "Wants=opencode.service\n"
        "After=network-online.target opencode.service\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workspace_dir}\n"
        f"EnvironmentFile={CONFIG_FILE}\n"
        f"ExecStart={exec_start} --foreground\n"
        "Restart=on-failure\n"
        f"RestartSec={SYSTEMD_RESTART_SEC}\n"
        f"TimeoutStartSec={SYSTEMD_TIMEOUT_START_SEC}\n"
        f"TimeoutStopSec={SYSTEMD_TIMEOUT_STOP_SEC}\n"
        f"StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}\n"
        f"StartLimitBurst={SYSTEMD_START_LIMIT_BURST}\n"
        f"StartLimitAction={SYSTEMD_START_LIMIT_ACTION}\n"
        f"WatchdogSec={SYSTEMD_WATCHDOG_SEC}\n"
        "NoNewPrivileges=true\n"
        "ProtectSystem=full\n"
        "ProtectHome=true\n"
        "PrivateTmp=true\n"
        "PrivateDevices=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _build_opencode_systemd_unit(workspace_dir: Path) -> str:
    opencode_executable = shutil.which("opencode") or "opencode"

    return (
        "[Unit]\n"
        "Description=OpenCode API Server\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={workspace_dir}\n"
        f"EnvironmentFile={OPENCODE_CONFIG_FILE}\n"
        f"ExecStart={opencode_executable} serve --hostname 127.0.0.1 --port 4096\n"
        "Restart=on-failure\n"
        f"RestartSec={SYSTEMD_RESTART_SEC}\n"
        f"TimeoutStartSec={SYSTEMD_TIMEOUT_START_SEC}\n"
        f"TimeoutStopSec={SYSTEMD_TIMEOUT_STOP_SEC}\n"
        f"StartLimitIntervalSec={SYSTEMD_START_LIMIT_INTERVAL_SEC}\n"
        f"StartLimitBurst={SYSTEMD_START_LIMIT_BURST}\n"
        f"StartLimitAction={SYSTEMD_START_LIMIT_ACTION}\n"
        f"WatchdogSec={SYSTEMD_WATCHDOG_SEC}\n"
        "NoNewPrivileges=true\n"
        "ProtectSystem=full\n"
        "ProtectHome=true\n"
        "PrivateTmp=true\n"
        "PrivateDevices=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _render_systemd_units(workspace_dir: Path) -> Dict[str, str]:
    return {
        SYSTEMD_UNIT_NAME: _build_systemd_unit(workspace_dir),
        OPENCODE_SYSTEMD_UNIT_NAME: _build_opencode_systemd_unit(workspace_dir),
    }


def _systemctl(*args: str, check: bool = True) -> None:
    if shutil.which("systemctl") is None:
        raise FileNotFoundError("systemctl not found")
    subprocess.run(["systemctl", "--user", *args], check=check)


def _missing_dependencies() -> Dict[str, Dict[str, object]]:
    missing: Dict[str, Dict[str, object]] = {}
    for name, spec in REQUIRED_DEPENDENCIES.items():
        binary = str(spec["binary"])
        if shutil.which(binary) is None:
            missing[name] = spec
    return missing


def _install_missing_dependencies(missing: Dict[str, Dict[str, object]]) -> None:
    if not missing:
        return

    print("Checking required runtime dependencies...")
    print("Missing dependencies:")
    for name, spec in missing.items():
        print(f"- {name} (binary: {spec['binary']})")

    install_now = _prompt("Install missing dependencies now? [Y/n]", "Y").lower()
    if install_now not in {"", "y", "yes"}:
        print("Skipping dependency installation. Setup will continue.")
        return

    for name, spec in missing.items():
        install_commands = spec.get("install_commands", [])
        if not isinstance(install_commands, list) or not install_commands:
            print(f"Could not auto-install {name}: {spec.get('manual_hint', 'Install manually.')}")
            continue

        if shutil.which("npm") is None:
            print(f"Could not auto-install {name}: npm is not available.")
            print(str(spec.get("manual_hint", "Install manually.")))
            continue

        installed = False
        for command in install_commands:
            if not isinstance(command, list) or not command:
                continue

            rendered = " ".join(command)
            print(f"Installing {name}: {rendered}")
            try:
                subprocess.run(command, check=True)
            except Exception as exc:
                print(f"Install command failed for {name}: {exc}")
                continue

            binary = str(spec["binary"])
            if shutil.which(binary) is not None:
                installed = True
                print(f"Installed {name} successfully.")
                break

        if not installed:
            print(f"Could not verify installation for {name}.")
            print(str(spec.get("manual_hint", "Install manually.")))

    remaining = _missing_dependencies()
    if remaining:
        print("Some required dependencies are still missing:")
        for dep in remaining:
            print(f"- {dep}")
        print("You can continue setup, but runtime commands may fail until these are installed.")


def _show_banner() -> None:
    # Avoid noisy ANSI output in non-interactive contexts.
    if not sys.stdout.isatty():
        return

    banner_text = _load_banner_text()
    if not banner_text:
        return

    sys.stdout.write(banner_text)


def _install_opencode_systemd_unit(workspace_dir: Path) -> None:
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    OPENCODE_SYSTEMD_UNIT_FILE.write_text(_build_opencode_systemd_unit(workspace_dir), encoding="utf-8")


def _ensure_opencode_service(workspace_dir: Path, config_path: Path = CONFIG_FILE) -> None:
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run: openbridge setup")
        raise SystemExit(1)

    _sync_opencode_env_from_bridge_config(config_path)

    _install_opencode_systemd_unit(workspace_dir)

    if shutil.which("systemctl") is None:
        print("systemctl not found; wrote the OpenCode service unit for manual management.")
        print(f"Wrote {OPENCODE_SYSTEMD_UNIT_FILE}")
        return

    _systemctl("daemon-reload")
    print(f"Wrote {OPENCODE_SYSTEMD_UNIT_FILE}")


def _workflow_config_from_args(args: argparse.Namespace) -> BridgeConfig:
    config_path = Path(getattr(args, "config", CONFIG_FILE))
    overrides: Dict[str, str] = {}
    if getattr(args, "debug", False):
        overrides["LOG_LEVEL"] = "DEBUG"
    if getattr(args, "log_level", None):
        overrides["LOG_LEVEL"] = str(args.log_level)
    return _merged_config(config_path, overrides)


def _workflow_manager_from_args(args: argparse.Namespace) -> WorkflowManager:
    from .opencode_bridge import OpenCodeBridge

    config = _workflow_config_from_args(args)
    workflows_file = Path(getattr(args, "workflows_file", WORKFLOWS_FILE))
    state_file = Path(getattr(args, "state_file", WORKFLOWS_STATE_FILE))
    return create_manager(
        config,
        OpenCodeBridge(config),
        workflows_file=workflows_file,
        state_file=state_file,
    )


def workflows_init_command(args: argparse.Namespace) -> None:
    workflows_file = Path(getattr(args, "workflows_file", WORKFLOWS_FILE))
    force = getattr(args, "force", False)
    if workflows_file.exists() and not force:
        print(f"Workflow file already exists: {workflows_file}")
        print("Use --force to overwrite it.")
        return

    payload = sample_workflows()
    save_workflows(workflows_file, payload)
    print(f"Wrote sample workflows to {workflows_file}")

    workflows = payload.get("workflows", []) if isinstance(payload, dict) else []
    placeholder_workflows = []
    for workflow in workflows:
        if not isinstance(workflow, dict):
            continue
        targets = workflow.get("targets", [])
        if isinstance(targets, list):
            for target in targets:
                try:
                    if int(target) == 0:
                        placeholder_workflows.append(str(workflow.get("id", "unknown")))
                        break
                except (TypeError, ValueError):
                    continue

    if placeholder_workflows:
        workflow_list = ", ".join(sorted(placeholder_workflows))
        print(
            "Warning: sample workflows still use placeholder Telegram targets (0). "
            f"Replace them before enabling the workflows: {workflow_list}"
        )


def workflows_validate_command(args: argparse.Namespace) -> None:
    workflows_file = Path(getattr(args, "workflows_file", WORKFLOWS_FILE))
    try:
        load_workflows(workflows_file)
    except Exception as exc:
        print(f"Workflow validation error: {exc}")
        raise SystemExit(1)

    print(f"Workflows file is valid: {workflows_file}")


def deploy_validate_command(args: argparse.Namespace) -> None:
    config_path = Path(getattr(args, "config", CONFIG_FILE))
    workspace_dir = Path(getattr(args, "workspace", Path.cwd())).resolve()

    if not config_path.exists():
        print(f"Config not found: {config_path}")
        raise SystemExit(1)

    try:
        config = _merged_config(config_path)
    except Exception as exc:
        print(f"Deployment validation error: {exc}")
        raise SystemExit(1)

    errors: list[str] = []
    warnings: list[str] = []

    if not workspace_dir.exists():
        errors.append(f"Workspace not found: {workspace_dir}")
    elif not workspace_dir.is_dir():
        errors.append(f"Workspace is not a directory: {workspace_dir}")

    working_dir = Path(config.opencode_working_dir).expanduser()
    if not working_dir.exists():
        errors.append(f"OpenCode working dir does not exist: {working_dir}")
    elif not working_dir.is_dir():
        errors.append(f"OpenCode working dir is not a directory: {working_dir}")

    if not OPENCODE_CONFIG_FILE.exists():
        errors.append(f"OpenCode service env not found: {OPENCODE_CONFIG_FILE}")

    if config.allow_all_chats and config.allowed_chat_ids:
        warnings.append("TELEGRAM_ALLOW_ALL_CHATS overrides TELEGRAM_ALLOWED_CHAT_IDS")
    elif not config.allow_all_chats and not config.allowed_chat_ids:
        warnings.append(
            "TELEGRAM_ALLOWED_CHAT_IDS is empty; the bot will reject all chats unless TELEGRAM_ALLOW_ALL_CHATS is set"
        )

    rendered_units = _render_systemd_units(workspace_dir)
    unit_text = rendered_units[SYSTEMD_UNIT_NAME]
    opencode_unit_text = rendered_units[OPENCODE_SYSTEMD_UNIT_NAME]
    if f"WorkingDirectory={workspace_dir}" not in unit_text:
        errors.append(f"Rendered {SYSTEMD_UNIT_NAME} does not target the requested workspace")
    if f"WorkingDirectory={workspace_dir}" not in opencode_unit_text:
        errors.append(f"Rendered {OPENCODE_SYSTEMD_UNIT_NAME} does not target the requested workspace")

    for warning in warnings:
        print(f"Warning: {warning}")

    if errors:
        for error in errors:
            print(f"Deployment validation error: {error}")
        raise SystemExit(1)

    print(f"Deployment validation passed for {workspace_dir}")


def workflows_list_command(args: argparse.Namespace) -> None:
    workflows_file = Path(getattr(args, "workflows_file", WORKFLOWS_FILE))
    state_file = Path(getattr(args, "state_file", WORKFLOWS_STATE_FILE))
    try:
        workflows = load_workflows(workflows_file)
    except Exception as exc:
        print(f"Workflow file error: {exc}")
        raise SystemExit(1)

    state_store = WorkflowStateStore(state_file)

    if not workflows:
        print(f"No workflows found at {workflows_file}")
        return

    now = time.time()
    lines = [f"Workflows file: {workflows_file}"]
    for workflow in workflows:
        state = state_store.get(workflow.id)
        state.next_run_at = _next_run_timestamp(workflow, state, now)
        status = state.last_status
        enabled = "enabled" if workflow.enabled else "disabled"
        lines.append(
            f"- {workflow.id}: {workflow.name} | {enabled} | schedule={workflow.schedule} | "
            f"last={_format_timestamp(state.last_run_at)} | next={_format_timestamp(state.next_run_at)} | "
            f"status={status} | targets={workflow.targets or 'none'}"
        )
    print("\n".join(lines))


def workflows_run_command(args: argparse.Namespace) -> None:
    from telegram import Bot

    workflow_id = getattr(args, "id", "").strip()
    if not workflow_id:
        print("Missing workflow id")
        raise SystemExit(1)

    manager = _workflow_manager_from_args(args)
    telegram_bot = Bot(token=manager.config.telegram_token)
    result = asyncio.run(manager.run_workflow(workflow_id, telegram_bot=telegram_bot, manual=True))
    if result.status == "success":
        print(f"Workflow {workflow_id} completed in {result.duration_seconds:.2f}s")
        if result.output:
            print(result.output)
        return

    if result.status == "skipped":
        print(f"Workflow {workflow_id} skipped: {result.skipped_reason}")
        return

    print(f"Workflow {workflow_id} failed: {result.error}")
    raise SystemExit(1)


def workflows_pause_command(args: argparse.Namespace) -> None:
    workflow_id = getattr(args, "id", "").strip()
    if not workflow_id:
        print("Missing workflow id")
        raise SystemExit(1)

    manager = _workflow_manager_from_args(args)
    manager.set_paused(workflow_id, True)
    print(f"Paused workflow: {workflow_id}")


def workflows_resume_command(args: argparse.Namespace) -> None:
    workflow_id = getattr(args, "id", "").strip()
    if not workflow_id:
        print("Missing workflow id")
        raise SystemExit(1)

    manager = _workflow_manager_from_args(args)
    manager.set_paused(workflow_id, False)
    print(f"Resumed workflow: {workflow_id}")


def workflows_status_command(args: argparse.Namespace) -> None:
    workflow_id = getattr(args, "id", "").strip()
    if not workflow_id:
        print("Missing workflow id")
        raise SystemExit(1)

    manager = _workflow_manager_from_args(args)
    print(manager.status_text(workflow_id))


def install_systemd_command(args: argparse.Namespace) -> None:
    workspace_dir = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()

    if not CONFIG_FILE.exists():
        print(f"Config not found: {CONFIG_FILE}")
        print("Run: openbridge setup")
        raise SystemExit(1)

    _sync_opencode_env_from_bridge_config(CONFIG_FILE)

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    rendered_units = _render_systemd_units(workspace_dir)
    OPENCODE_SYSTEMD_UNIT_FILE.write_text(rendered_units[OPENCODE_SYSTEMD_UNIT_NAME], encoding="utf-8")
    SYSTEMD_UNIT_FILE.write_text(rendered_units[SYSTEMD_UNIT_NAME], encoding="utf-8")

    print(f"Installed systemd units to {SYSTEMD_UNIT_FILE} and {OPENCODE_SYSTEMD_UNIT_FILE}")

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


def render_systemd_command(args: argparse.Namespace) -> None:
    workspace_dir = Path(args.workspace).resolve() if args.workspace else Path.cwd().resolve()

    if not workspace_dir.exists():
        print(f"Workspace not found: {workspace_dir}")
        raise SystemExit(1)

    rendered_units = _render_systemd_units(workspace_dir)
    print(f"# {SYSTEMD_UNIT_NAME}")
    print(rendered_units[SYSTEMD_UNIT_NAME], end="")
    print(f"# {OPENCODE_SYSTEMD_UNIT_NAME}")
    print(rendered_units[OPENCODE_SYSTEMD_UNIT_NAME], end="")


def uninstall_systemd_command(_: argparse.Namespace) -> None:
    unit_exists = SYSTEMD_UNIT_FILE.exists()
    opencode_unit_exists = OPENCODE_SYSTEMD_UNIT_FILE.exists()
    if shutil.which("systemctl") is not None:
        subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], check=False)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)

    if unit_exists:
        SYSTEMD_UNIT_FILE.unlink()
        print(f"Removed {SYSTEMD_UNIT_FILE}")
    else:
        print(f"No systemd unit found at {SYSTEMD_UNIT_FILE}")

    if opencode_unit_exists:
        OPENCODE_SYSTEMD_UNIT_FILE.unlink()
        print(f"Removed {OPENCODE_SYSTEMD_UNIT_FILE}")
    else:
        print(f"No systemd unit found at {OPENCODE_SYSTEMD_UNIT_FILE}")

    if shutil.which("systemctl") is None:
        print("systemctl not found; remove the unit manually if needed.")
    else:
        print(f"Disabled {SYSTEMD_UNIT_NAME} and reloaded user systemd")


def setup_command(_: argparse.Namespace) -> None:
    _show_banner()
    print("Telegram OpenCode Bridge Setup")
    print("================================")

    _install_missing_dependencies(_missing_dependencies())

    current = _with_legacy_openbridge_aliases(read_env_file(CONFIG_FILE))
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
    config["OPENCODE_API_BASE_URL"] = _prompt(
        "OpenCode API base URL",
        current.get("OPENCODE_API_BASE_URL", "http://127.0.0.1:4096"),
    )
    config["OPENCODE_API_USERNAME"] = _prompt(
        "OpenCode API username",
        current.get("OPENCODE_API_USERNAME", "opencode"),
    )
    config["OPENCODE_API_PASSWORD"] = _prompt(
        "OpenCode API password (optional)",
        current.get("OPENCODE_API_PASSWORD", ""),
        secret=True,
        display_default="<hidden>" if current.get("OPENCODE_API_PASSWORD", "").strip() else "",
    )
    config["OPENCODE_API_TIMEOUT_SECONDS"] = _prompt(
        "OpenCode API timeout seconds",
        current.get("OPENCODE_API_TIMEOUT_SECONDS", "120"),
    )
    config["OPENBRIDGE_CHAT_QUEUE_MAX_PENDING"] = _prompt(
        "Per-chat queue depth",
        current.get("OPENBRIDGE_CHAT_QUEUE_MAX_PENDING", "5"),
    )
    queue_overflow_default = current.get("OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE", "reject")
    queue_overflow_mode = _prompt(
        "Per-chat queue overflow mode [reject/drop_oldest]",
        queue_overflow_default,
    ).strip().lower()
    if queue_overflow_mode not in {"reject", "drop_oldest"}:
        queue_overflow_mode = "reject"
    config["OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE"] = queue_overflow_mode
    config["OPENCODE_SERVER_USERNAME"] = _prompt(
        "OpenCode server username (optional)",
        current.get("OPENCODE_SERVER_USERNAME", ""),
    )
    config["OPENCODE_SERVER_PASSWORD"] = _prompt(
        "OpenCode server password (optional)",
        current.get("OPENCODE_SERVER_PASSWORD", ""),
        secret=True,
        display_default="<hidden>" if current.get("OPENCODE_SERVER_PASSWORD", "").strip() else "",
    )
    config["LOG_LEVEL"] = _prompt("Log level", current.get("LOG_LEVEL", "INFO"))

    def _chat_ids_default(value: str) -> str:
        if not value.strip():
            return ""
        count = len([item for item in value.split(",") if item.strip()])
        return f"<set:{count} id(s)>"

    config["TELEGRAM_ALLOWED_CHAT_IDS"] = _prompt(
        "Allowed chat ids (comma-separated, blank = reject all)",
        current.get("TELEGRAM_ALLOWED_CHAT_IDS", ""),
        display_default=_chat_ids_default(current.get("TELEGRAM_ALLOWED_CHAT_IDS", "")),
    )
    allow_all_default = "Y" if current.get("TELEGRAM_ALLOW_ALL_CHATS", "0") in {"1", "true", "yes", "on"} else "N"
    config["TELEGRAM_ALLOW_ALL_CHATS"] = "1" if _prompt(
        "Allow all chats? [y/N]",
        allow_all_default,
    ).lower() in {"y", "yes"} else "0"

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

    configure_llm_role("OPENBRIDGE_INPUT_LLM", "input prompt enhancer")
    configure_llm_role("OPENBRIDGE_OUTPUT_LLM", "output prettifier")

    # Keep legacy decorator keys for backwards compatibility, but default them off in new setups.
    config["OPENBRIDGE_DECORATOR_ENABLED"] = "0"
    config["OPENBRIDGE_DECORATOR_API_KEY"] = ""
    config["OPENBRIDGE_DECORATOR_MODEL"] = ""
    config["OPENBRIDGE_DECORATOR_BASE_URL"] = ""
    config["OPENBRIDGE_DECORATOR_TIMEOUT_SECONDS"] = "30"

    write_env_file(CONFIG_FILE, config)
    print(f"Saved configuration to {CONFIG_FILE}")

    _sync_opencode_env_from_bridge_config(CONFIG_FILE)
    print(f"Saved OpenCode service configuration to {OPENCODE_CONFIG_FILE}")

    workspace_dir = Path(config["OPENCODE_WORKING_DIR"]).resolve()
    if shutil.which("systemctl") is not None:
        try:
            _ensure_opencode_service(workspace_dir)
        except Exception as exc:
            print(f"Could not install the OpenCode systemd service: {exc}")
    else:
        print("systemctl not found; OpenCode service was not installed automatically.")

    start_now = _prompt("Start the app now? [Y/n]", "Y").lower()
    if start_now in {"", "y", "yes"}:
        start_command(argparse.Namespace(config=CONFIG_FILE, foreground=False, debug=False, log_level=None))


def start_command(args: argparse.Namespace) -> None:
    _show_banner()
    print(f"Telegram ↔ OpenCode Bridge  •  v{__version__}")
    config_path = Path(args.config) if args.config else CONFIG_FILE
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run: openbridge setup")
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

    if shutil.which("systemctl") is None:
        print("systemctl not found; assuming OpenCode server is already running.")

    foreground = getattr(args, "foreground", False)
    stop_event = threading.Event()
    previous_signal_handlers = _install_signal_handlers(stop_event)
    try:
        if not foreground:
            daemon_pid = _daemonize(LOG_FILE)
            if daemon_pid is not None:
                deadline = time.time() + 5
                active_pid: Optional[int] = None
                while time.time() < deadline:
                    active_pid = _load_pid()
                    if active_pid is not None:
                        break
                    time.sleep(0.05)

                print(f"OpenBridge is running with PID {active_pid or daemon_pid}")
                return

            _write_pid()
            print(f"OpenBridge is running with PID {os.getpid()}")
        else:
            print(f"OpenBridge is running in foreground with PID {os.getpid()}")

        try:
            run_bridge(config, foreground=foreground, log_file=LOG_FILE, stop_event=stop_event)
        finally:
            if not foreground:
                _remove_pid()
    finally:
        _restore_signal_handlers(previous_signal_handlers)


def _find_openbridge_pids() -> set[int]:
    pids: set[int] = set()
    ps_result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        capture_output=True,
        text=True,
        check=False,
    )
    if ps_result.returncode != 0:
        return pids

    stdout = ps_result.stdout if isinstance(ps_result.stdout, str) else ""
    current_pid = os.getpid()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split(None, 1)
        if len(parts) != 2:
            continue

        pid_text, cmdline = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue

        if pid == current_pid:
            continue

        cmdline_lc = cmdline.lower()
        if "openbridge stop" in cmdline_lc:
            continue

        is_openbridge_process = (
            "openbridge.app start" in cmdline_lc
            or "openbridge start" in cmdline_lc
            or "python -m openbridge.app" in cmdline_lc
        )
        if is_openbridge_process:
            pids.add(pid)

    return pids


def _wait_for_exit(pids: set[int], timeout_seconds: float = 3.0) -> set[int]:
    deadline = time.time() + timeout_seconds
    remaining = {pid for pid in pids if is_process_alive(pid)}
    while remaining and time.time() < deadline:
        time.sleep(0.1)
        remaining = {pid for pid in remaining if is_process_alive(pid)}
    return remaining


def stop_command(args: argparse.Namespace) -> None:
    force = getattr(args, "force", False)

    if shutil.which("systemctl") is not None:
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME], check=False)

    candidates: set[int] = set()
    pid = _load_pid()
    if pid and pid > 0:
        candidates.add(pid)
    candidates.update(_find_openbridge_pids())

    if not candidates:
        _remove_pid()
        print("No running OpenBridge process found.")
        return

    for process_pid in sorted(candidates):
        try:
            os.kill(process_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"No permission to signal PID {process_pid}; skipping")

    remaining = _wait_for_exit(candidates)
    if remaining and force:
        for process_pid in sorted(remaining):
            try:
                os.kill(process_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                print(f"No permission to kill PID {process_pid}; skipping")
        remaining = _wait_for_exit(remaining, timeout_seconds=1.0)

    _remove_pid()
    stopped_count = len(candidates) - len(remaining)
    if remaining:
        print(f"Stopped {stopped_count} OpenBridge process(es); still running: {sorted(remaining)}")
        if not force:
            print("Use --force to send SIGKILL to stuck processes.")
        return

    print(f"OpenBridge stopped ({stopped_count} process(es) terminated).")


def status_command(_: argparse.Namespace) -> None:
    pid = _load_pid()
    if pid:
        print(f"Running in background with PID {pid}")
        return

    print("Not running.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openbridge", description="Telegram OpenCode Bridge")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
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
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Send SIGKILL to any OpenBridge process that ignores SIGTERM",
    )
    stop_parser.set_defaults(func=stop_command)

    status_parser = subparsers.add_parser("status", help="Show whether the bridge is running")
    status_parser.set_defaults(func=status_command)

    workflows_parser = subparsers.add_parser("workflows", help="Manage recurring workflows")
    workflows_parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Path to bridge env file")
    workflows_parser.add_argument(
        "--workflows-file",
        type=Path,
        default=WORKFLOWS_FILE,
        help="Path to the workflows definition file",
    )
    workflows_parser.add_argument(
        "--state-file",
        type=Path,
        default=WORKFLOWS_STATE_FILE,
        help="Path to the workflow state file",
    )
    workflows_subparsers = workflows_parser.add_subparsers(dest="workflow_command")

    workflows_init_parser = workflows_subparsers.add_parser("init", help="Create a sample workflows file")
    workflows_init_parser.add_argument("--force", action="store_true", help="Overwrite an existing workflows file")
    workflows_init_parser.set_defaults(func=workflows_init_command)

    workflows_list_parser = workflows_subparsers.add_parser("list", help="List configured workflows")
    workflows_list_parser.set_defaults(func=workflows_list_command)

    workflows_validate_parser = workflows_subparsers.add_parser("validate", help="Validate the workflows file")
    workflows_validate_parser.set_defaults(func=workflows_validate_command)

    workflows_run_parser = workflows_subparsers.add_parser("run", help="Run a workflow immediately")
    workflows_run_parser.add_argument("--id", required=True, help="Workflow id to run")
    workflows_run_parser.set_defaults(func=workflows_run_command)

    workflows_pause_parser = workflows_subparsers.add_parser("pause", help="Pause a workflow")
    workflows_pause_parser.add_argument("--id", required=True, help="Workflow id to pause")
    workflows_pause_parser.set_defaults(func=workflows_pause_command)

    workflows_resume_parser = workflows_subparsers.add_parser("resume", help="Resume a workflow")
    workflows_resume_parser.add_argument("--id", required=True, help="Workflow id to resume")
    workflows_resume_parser.set_defaults(func=workflows_resume_command)

    workflows_status_parser = workflows_subparsers.add_parser("status", help="Show workflow status")
    workflows_status_parser.add_argument("--id", required=True, help="Workflow id to inspect")
    workflows_status_parser.set_defaults(func=workflows_status_command)

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

    render_systemd_parser = subparsers.add_parser(
        "render-systemd",
        help="Render the systemd units for the current host without writing files",
    )
    render_systemd_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory to resolve into the rendered units",
    )
    render_systemd_parser.set_defaults(func=render_systemd_command)

    deploy_validate_parser = subparsers.add_parser(
        "deploy-validate",
        help="Validate deployment config, workspace, and service paths",
    )
    deploy_validate_parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Path to config env file")
    deploy_validate_parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory to validate for systemd units",
    )
    deploy_validate_parser.set_defaults(func=deploy_validate_command)

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
