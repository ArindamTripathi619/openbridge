#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.openbridge import opencode_bridge  # noqa: E402


def _parse_env_example(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        key, value = line[len("export ") :].split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _require_key_value(values: dict[str, str], key: str, expected: str, errors: list[str]) -> None:
    actual = values.get(key)
    if actual is None:
        errors.append(f"Missing key in config/opencode-bridge.env.example: {key}")
        return
    if actual != expected:
        errors.append(
            f"Default drift for {key}: expected {expected!r} from runtime defaults, found {actual!r} in env example"
        )


def _require_readme_contains(readme_text: str, required: list[str], errors: list[str]) -> None:
    for needle in required:
        if needle not in readme_text:
            errors.append(f"README.md missing required sensitive/default reference: {needle}")


def main() -> int:
    env_example_path = ROOT / "config" / "opencode-bridge.env.example"
    readme_path = ROOT / "README.md"
    yaml_reference_path = ROOT / "config" / "example.yaml"

    env_values = _parse_env_example(env_example_path)
    readme_text = readme_path.read_text(encoding="utf-8")
    yaml_text = yaml_reference_path.read_text(encoding="utf-8")
    errors: list[str] = []

    expected_defaults = {
        "OPENCODE_API_BASE_URL": opencode_bridge.DEFAULT_OPENCODE_API_BASE_URL,
        "OPENCODE_API_TIMEOUT_SECONDS": str(opencode_bridge.DEFAULT_OPENCODE_API_TIMEOUT_SECONDS),
        "OPENBRIDGE_INPUT_LLM_LITELLM_PORT": str(opencode_bridge.DEFAULT_LITELLM_PORT),
        "OPENBRIDGE_OUTPUT_LLM_LITELLM_PORT": str(opencode_bridge.DEFAULT_LITELLM_PORT),
        "OPENBRIDGE_INPUT_LLM_TIMEOUT_SECONDS": str(opencode_bridge.DEFAULT_DECORATOR_TIMEOUT_SECONDS),
        "OPENBRIDGE_OUTPUT_LLM_TIMEOUT_SECONDS": str(opencode_bridge.DEFAULT_DECORATOR_TIMEOUT_SECONDS),
        "TELEGRAM_ALLOW_ALL_CHATS": "0",
    }

    for key, expected in expected_defaults.items():
        _require_key_value(env_values, key, expected, errors)

    # Known-sensitive references that must stay visible in docs.
    _require_readme_contains(
        readme_text,
        [
            "OPENCODE_API_PASSWORD",
            "OPENCODE_SERVER_PASSWORD",
            "TELEGRAM_ALLOW_ALL_CHATS",
            "OPENCODE_API_BASE_URL",
            "OPENCODE_API_TIMEOUT_SECONDS",
            "~/.config/openbridge/bridge.env",
            "~/.config/openbridge/opencode.env",
        ],
        errors,
    )

    # Ensure YAML reference still maps key sensitive config placeholders.
    yaml_required_patterns = [
        r"\$\{OPENCODE_API_BASE_URL\}",
        r"\$\{OPENCODE_API_TIMEOUT_SECONDS\}",
        r"\$\{TELEGRAM_ALLOW_ALL_CHATS\}",
        r"\$\{OPENBRIDGE_INPUT_LLM_TIMEOUT_SECONDS\}",
        r"\$\{OPENBRIDGE_OUTPUT_LLM_TIMEOUT_SECONDS\}",
    ]
    for pattern in yaml_required_patterns:
        if re.search(pattern, yaml_text) is None:
            errors.append(f"config/example.yaml missing expected placeholder: {pattern}")

    if errors:
        print("Config/docs drift check failed:")
        for item in errors:
            print(f"- {item}")
        return 1

    print("Config/docs drift check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())