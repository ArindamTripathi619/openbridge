"""Minimal Telegram -> OpenCode -> Telegram bridge.

This module provides a barebones bot flow:
1) receive text in Telegram
2) run `opencode run ... <prompt>` in background
3) send cleaned result back to Telegram chat
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger("opencode_bridge")

TELEGRAM_LIMIT = 4096
SAFE_CHUNK = 3900
DEFAULT_FALLBACK_MODELS = (
    "opencode/minimax-m2.5-free",
    "opencode/nemotron-3-super-free",
)
DEFAULT_DECORATOR_TIMEOUT_SECONDS = 30
DEFAULT_LITELLM_PORT = 8000
DEFAULT_LITELLM_MODEL = "groq-gpt-oss-mini"
SENSITIVE_LOG_PATTERNS = (
    re.compile(r"(https?://api\.telegram\.org/bot)(\d{6,12}:[A-Za-z0-9_-]+)(/)", re.IGNORECASE),
    re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{20,})\b"),
)


@dataclass
class BridgeConfig:
    telegram_token: str
    opencode_model: Optional[str]
    opencode_working_dir: str
    opencode_timeout_seconds: int
    max_concurrent_jobs: int
    allowed_chat_ids: Set[int]
    log_level: str = "INFO"
    decorator_enabled: bool = False
    decorator_api_key: Optional[str] = None
    decorator_model: Optional[str] = None
    decorator_base_url: Optional[str] = None
    decorator_timeout_seconds: int = DEFAULT_DECORATOR_TIMEOUT_SECONDS
    input_llm_enabled: bool = False
    input_llm_provider: str = "none"
    input_llm_api_key: Optional[str] = None
    input_llm_model: Optional[str] = None
    input_llm_base_url: Optional[str] = None
    input_llm_litellm_port: int = DEFAULT_LITELLM_PORT
    input_llm_timeout_seconds: int = DEFAULT_DECORATOR_TIMEOUT_SECONDS
    output_llm_enabled: bool = False
    output_llm_provider: str = "none"
    output_llm_api_key: Optional[str] = None
    output_llm_model: Optional[str] = None
    output_llm_base_url: Optional[str] = None
    output_llm_litellm_port: int = DEFAULT_LITELLM_PORT
    output_llm_timeout_seconds: int = DEFAULT_DECORATOR_TIMEOUT_SECONDS

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> "BridgeConfig":
        token = mapping.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Missing TELEGRAM_BOT_TOKEN")

        working_dir = mapping.get("OPENCODE_WORKING_DIR", ".").strip() or "."

        raw_chat_ids = mapping.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
        allowed_chat_ids: Set[int] = set()
        if raw_chat_ids:
            for raw in raw_chat_ids.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    allowed_chat_ids.add(int(raw))
                except ValueError as exc:
                    raise ValueError(f"Invalid chat id in TELEGRAM_ALLOWED_CHAT_IDS: {raw}") from exc

        timeout = int(mapping.get("OPENCODE_TIMEOUT_SECONDS", "600"))
        max_jobs = int(mapping.get("OPENCODE_MAX_CONCURRENT", "1"))
        if timeout <= 0:
            raise ValueError("OPENCODE_TIMEOUT_SECONDS must be > 0")
        if max_jobs <= 0:
            raise ValueError("OPENCODE_MAX_CONCURRENT must be > 0")

        (
            decorator_enabled,
            decorator_api_key,
            decorator_model,
            decorator_base_url,
            decorator_timeout_seconds,
        ) = _parse_legacy_decorator_config(mapping)

        (
            input_llm_enabled,
            input_llm_provider,
            input_llm_api_key,
            input_llm_model,
            input_llm_base_url,
            input_llm_litellm_port,
            input_llm_timeout_seconds,
        ) = _parse_llm_role_config(mapping, role="TELEWATCH_INPUT_LLM")

        (
            output_llm_enabled,
            output_llm_provider,
            output_llm_api_key,
            output_llm_model,
            output_llm_base_url,
            output_llm_litellm_port,
            output_llm_timeout_seconds,
        ) = _parse_llm_role_config(
            mapping,
            role="TELEWATCH_OUTPUT_LLM",
            legacy_enabled=decorator_enabled,
            legacy_api_key=decorator_api_key,
            legacy_model=decorator_model,
            legacy_base_url=decorator_base_url,
            legacy_timeout_seconds=decorator_timeout_seconds,
        )

        return cls(
            telegram_token=token,
            opencode_model=mapping.get("OPENCODE_MODEL", "").strip() or None,
            opencode_working_dir=working_dir,
            opencode_timeout_seconds=timeout,
            max_concurrent_jobs=max_jobs,
            allowed_chat_ids=allowed_chat_ids,
            log_level=(mapping.get("LOG_LEVEL", "INFO").strip() or "INFO").upper(),
            decorator_enabled=decorator_enabled,
            decorator_api_key=decorator_api_key,
            decorator_model=decorator_model,
            decorator_base_url=decorator_base_url,
            decorator_timeout_seconds=decorator_timeout_seconds,
            input_llm_enabled=input_llm_enabled,
            input_llm_provider=input_llm_provider,
            input_llm_api_key=input_llm_api_key,
            input_llm_model=input_llm_model,
            input_llm_base_url=input_llm_base_url,
            input_llm_litellm_port=input_llm_litellm_port,
            input_llm_timeout_seconds=input_llm_timeout_seconds,
            output_llm_enabled=output_llm_enabled,
            output_llm_provider=output_llm_provider,
            output_llm_api_key=output_llm_api_key,
            output_llm_model=output_llm_model,
            output_llm_base_url=output_llm_base_url,
            output_llm_litellm_port=output_llm_litellm_port,
            output_llm_timeout_seconds=output_llm_timeout_seconds,
        )

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls.from_mapping(os.environ)


def _build_opencode_command(config: BridgeConfig, prompt: str) -> List[str]:
    cmd = ["opencode", "run"]
    if config.opencode_model:
        cmd.extend(["--model", config.opencode_model])
    cmd.append(prompt)
    return cmd


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_llm_provider(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"api", "direct", "apikey", "api_key"}:
        return "api"
    if lowered == "litellm":
        return "litellm"
    return "none"


def _parse_legacy_decorator_config(
    mapping: Mapping[str, str],
) -> tuple[bool, Optional[str], Optional[str], Optional[str], int]:
    decorator_api_key = mapping.get("TELEWATCH_DECORATOR_API_KEY", "").strip() or None
    decorator_model = mapping.get("TELEWATCH_DECORATOR_MODEL", "").strip() or None
    decorator_base_url = mapping.get("TELEWATCH_DECORATOR_BASE_URL", "").strip() or None
    decorator_timeout_seconds = int(
        mapping.get("TELEWATCH_DECORATOR_TIMEOUT_SECONDS", str(DEFAULT_DECORATOR_TIMEOUT_SECONDS))
    )
    decorator_enabled = _parse_bool(mapping.get("TELEWATCH_DECORATOR_ENABLED", "0"))
    if decorator_api_key and decorator_model and decorator_base_url:
        decorator_enabled = True
    if decorator_enabled and (not decorator_api_key or not decorator_model or not decorator_base_url):
        decorator_enabled = False
    if decorator_timeout_seconds <= 0:
        raise ValueError("TELEWATCH_DECORATOR_TIMEOUT_SECONDS must be > 0")
    return (
        decorator_enabled,
        decorator_api_key,
        decorator_model,
        decorator_base_url,
        decorator_timeout_seconds,
    )


def _parse_llm_role_config(
    mapping: Mapping[str, str],
    *,
    role: str,
    legacy_enabled: bool = False,
    legacy_api_key: Optional[str] = None,
    legacy_model: Optional[str] = None,
    legacy_base_url: Optional[str] = None,
    legacy_timeout_seconds: int = DEFAULT_DECORATOR_TIMEOUT_SECONDS,
) -> tuple[bool, str, Optional[str], Optional[str], Optional[str], int, int]:
    enabled = _parse_bool(mapping.get(f"{role}_ENABLED", "0"))
    provider = _normalize_llm_provider(mapping.get(f"{role}_PROVIDER", ""))
    api_key = mapping.get(f"{role}_API_KEY", "").strip() or None
    model = mapping.get(f"{role}_MODEL", "").strip() or None
    base_url = mapping.get(f"{role}_BASE_URL", "").strip() or None
    litellm_port = int(mapping.get(f"{role}_LITELLM_PORT", str(DEFAULT_LITELLM_PORT)))
    timeout_seconds = int(mapping.get(f"{role}_TIMEOUT_SECONDS", str(DEFAULT_DECORATOR_TIMEOUT_SECONDS)))

    if role == "TELEWATCH_OUTPUT_LLM":
        if not api_key:
            api_key = legacy_api_key
        if not model:
            model = legacy_model
        if not base_url:
            base_url = legacy_base_url
        if not _parse_bool(mapping.get(f"{role}_ENABLED", "0")) and legacy_enabled:
            enabled = True
        if f"{role}_TIMEOUT_SECONDS" not in mapping:
            timeout_seconds = legacy_timeout_seconds

    if timeout_seconds <= 0:
        raise ValueError(f"{role}_TIMEOUT_SECONDS must be > 0")
    if litellm_port <= 0:
        raise ValueError(f"{role}_LITELLM_PORT must be > 0")

    if provider == "none" and enabled:
        if api_key and model and base_url:
            provider = "api"
        elif model:
            provider = "litellm"

    if provider == "api" and (not api_key or not model or not base_url):
        enabled = False
    elif provider == "litellm" and not model:
        enabled = False

    if provider == "none":
        enabled = False

    return enabled, provider, api_key, model, base_url, litellm_port, timeout_seconds


def _redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_LOG_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]{match.group(3)}" if match.lastindex and match.lastindex >= 3 else "[REDACTED]", redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _redact_sensitive_text(rendered)


def _is_quota_error(text: str) -> bool:
    lowered = text.lower()
    quota_markers = (
        "quota",
        "rate limit",
        "rate-limit",
        "insufficient credits",
        "billing",
        "429",
        "limit exceeded",
        "too many requests",
        "free tier exhausted",
    )
    return any(marker in lowered for marker in quota_markers)


def _model_candidates(primary_model: Optional[str]) -> List[Optional[str]]:
    candidates: List[Optional[str]] = []
    for model in (primary_model, *DEFAULT_FALLBACK_MODELS):
        if model and model not in candidates:
            candidates.append(model)
    if not candidates:
        candidates.append(None)
    return candidates


def _clean_opencode_output(output: str) -> str:
    """Strip noisy tool-call lines and keep user-facing final text."""
    lines = output.splitlines()
    in_cmd = False
    buffer: List[str] = []

    for line in lines:
        if re.match(r"^[\s]*\$", line):
            in_cmd = True
            buffer = []
            continue

        if re.match(r"^[✱✗!]", line) or line.startswith("DEBUG:") or line.startswith("> "):
            buffer = []
            continue

        if in_cmd and re.match(r"^[\s]*$", line):
            in_cmd = False
            continue

        if not in_cmd:
            buffer.append(line)

    cleaned = "\n".join(buffer).strip()
    if cleaned:
        return cleaned

    return output.strip() or "OpenCode returned no output."


def _chunk_message(text: str, limit: int = SAFE_CHUNK) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return

    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        chunk = text[start:end]

        if end < len(text):
            split = chunk.rfind("\n")
            if split > 100:
                end = start + split
                chunk = text[start:end]

        yield chunk.strip() or "(empty)"
        start = end


class OpenCodeBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
        self._started_at = time.monotonic()
        self._stats = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "quota_fallbacks": 0,
            "prompt_rewrites": 0,
            "input_llm_failures": 0,
            "decorated_outputs": 0,
            "decorator_failures": 0,
            "last_model": None,
            "last_error": None,
            "last_request_at": None,
            "last_success_at": None,
            "last_result_kind": None,
        }

    async def run_prompt(self, prompt: str) -> str:
        self._stats["requests"] += 1
        self._stats["last_request_at"] = time.time()
        last_quota_error = ""

        for model in _model_candidates(self.config.opencode_model):
            attempt_config = BridgeConfig(
                telegram_token=self.config.telegram_token,
                opencode_model=model,
                opencode_working_dir=self.config.opencode_working_dir,
                opencode_timeout_seconds=self.config.opencode_timeout_seconds,
                max_concurrent_jobs=self.config.max_concurrent_jobs,
                allowed_chat_ids=self.config.allowed_chat_ids,
                log_level=self.config.log_level,
            )

            result = await self._run_prompt_once(attempt_config, prompt)
            if result is None:
                continue

            if result.startswith("__QUOTA__:"):
                last_quota_error = result[len("__QUOTA__:") :].strip()
                self._stats["quota_fallbacks"] += 1
                self._stats["last_model"] = model or "default"
                logger.warning(
                    "OpenCode quota hit for model %s; trying fallback if available",
                    model or "default",
                )
                continue

            self._stats["last_model"] = model or "default"
            if self._is_error_result(result):
                self._stats["failed_requests"] += 1
                self._stats["last_error"] = result
                self._stats["last_result_kind"] = "error"
            else:
                self._stats["successful_requests"] += 1
                self._stats["last_success_at"] = time.time()
                self._stats["last_error"] = None
                self._stats["last_result_kind"] = "success"

            return result

        if last_quota_error:
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = last_quota_error
            self._stats["last_result_kind"] = "quota"
            return (
                "All configured OpenCode models appear to be quota-limited or rate-limited right now. "
                "Tried the primary model and free fallbacks.\n"
                f"\n{last_quota_error}"
            )

        self._stats["failed_requests"] += 1
        self._stats["last_error"] = "OpenCode returned no output."
        self._stats["last_result_kind"] = "empty"
        return "OpenCode returned no output."

    def _resolve_llm_runtime(self, stage: str) -> Optional[dict]:
        if stage == "input":
            enabled = self.config.input_llm_enabled
            provider = self.config.input_llm_provider
            model = self.config.input_llm_model
            api_key = self.config.input_llm_api_key
            base_url = self.config.input_llm_base_url
            litellm_port = self.config.input_llm_litellm_port
            timeout_seconds = self.config.input_llm_timeout_seconds
        else:
            enabled = self.config.output_llm_enabled
            provider = self.config.output_llm_provider
            model = self.config.output_llm_model
            api_key = self.config.output_llm_api_key
            base_url = self.config.output_llm_base_url
            litellm_port = self.config.output_llm_litellm_port
            timeout_seconds = self.config.output_llm_timeout_seconds

            if not enabled and self.config.decorator_enabled:
                enabled = True
                provider = "api"
                model = model or self.config.decorator_model
                api_key = api_key or self.config.decorator_api_key
                base_url = base_url or self.config.decorator_base_url
                timeout_seconds = self.config.decorator_timeout_seconds

        if not enabled or not model:
            return None

        if provider == "litellm":
            return {
                "model": model,
                "api_key": api_key or "sk-local",
                "base_url": f"http://localhost:{litellm_port}/v1",
                "timeout_seconds": timeout_seconds,
            }

        if provider == "api" and api_key and base_url:
            return {
                "model": model,
                "api_key": api_key,
                "base_url": base_url,
                "timeout_seconds": timeout_seconds,
            }

        return None

    async def enhance_prompt(self, raw_prompt: str) -> str:
        runtime = self._resolve_llm_runtime("input")
        if not runtime:
            return raw_prompt

        try:
            rewritten = await asyncio.to_thread(self._enhance_prompt_sync, runtime, raw_prompt)
        except Exception:
            self._stats["input_llm_failures"] += 1
            logger.exception("Input LLM rewrite failed")
            return raw_prompt

        if not rewritten:
            self._stats["input_llm_failures"] += 1
            return raw_prompt

        self._stats["prompt_rewrites"] += 1
        return rewritten

    def _enhance_prompt_sync(self, runtime: dict, raw_prompt: str) -> Optional[str]:
        payload = {
            "model": runtime["model"],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You rewrite user requests into high-signal prompts for OpenCode. "
                        "Preserve intent, constraints, and expected output. "
                        "Return plain text only, no markdown, no commentary."
                    ),
                },
                {
                    "role": "user",
                    "content": raw_prompt,
                },
            ],
            "temperature": 0.1,
        }

        content = self._call_chat_completion(runtime, payload)
        if not content:
            return None

        candidate = content.strip()
        if not candidate:
            return None

        return candidate[:8000]

    def _is_error_result(self, text: str) -> bool:
        error_prefixes = (
            "OpenCode timed out.",
            "OpenCode failed",
            "OpenCode could not use",
            "OpenCode rejected",
            "All configured OpenCode models",
            "opencode binary not found",
            "OpenCode returned no output.",
        )
        return text.startswith(error_prefixes)

    async def _run_prompt_once(self, config: BridgeConfig, prompt: str) -> Optional[str]:
        cmd = _build_opencode_command(config, prompt)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=config.opencode_working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=config.opencode_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                "OpenCode timed out. "
                f"Try a smaller prompt or increase OPENCODE_TIMEOUT_SECONDS (current: {config.opencode_timeout_seconds})."
            )

        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace").strip()

        cleaned = _clean_opencode_output(out_text)
        if proc.returncode != 0:
            if _is_quota_error(err_text + "\n" + cleaned):
                return f"__QUOTA__:{err_text or cleaned or f'OpenCode failed with exit code {proc.returncode}.'}"

            if "insufficient permissions" in err_text.lower() or "forbidden" in err_text.lower():
                return (
                    "OpenCode could not use the configured model because of a permission/configuration error. "
                    "Try unsetting OPENCODE_MODEL or updating your OpenCode provider permissions.\n"
                    f"\n{err_text}"
                )

            if "opencode run [message..]" in err_text.lower() or "usage:" in err_text.lower():
                return (
                    "OpenCode rejected the command line arguments. The bridge now uses the supported current CLI format, "
                    "so this usually means the local OpenCode install is out of sync or missing.\n"
                    f"\n{err_text}"
                )

            if err_text:
                return f"OpenCode failed (exit={proc.returncode}):\n{err_text}"
            return f"OpenCode failed with exit code {proc.returncode}."

        if err_text and "warning" in err_text.lower():
            logger.warning("OpenCode warning: %s", err_text)

        return cleaned

    def _is_decorated_output_enabled(self) -> bool:
        return self._resolve_llm_runtime("output") is not None

    async def decorate_output(self, raw_output: str) -> Optional[List[str]]:
        if not self._is_decorated_output_enabled():
            return None

        if self._is_error_result(raw_output):
            return None

        try:
            payload = await asyncio.to_thread(self._decorate_output_sync, raw_output)
        except Exception:
            self._stats["decorator_failures"] += 1
            logger.exception("Decorator post-processor failed")
            return None

        if not payload:
            self._stats["decorator_failures"] += 1
            return None

        sections = self._render_decorated_messages(payload)
        if not sections:
            self._stats["decorator_failures"] += 1
            return None

        self._stats["decorated_outputs"] += 1
        return sections

    def _decorate_output_sync(self, raw_output: str) -> Optional[dict]:
        runtime = self._resolve_llm_runtime("output")
        if not runtime:
            return None

        prompt = (
            "Transform the following OpenCode result into a concise Telegram-friendly JSON object. "
            "Return JSON only, with exactly these keys: title, summary, highlights, actions, warnings. "
            "Use short, practical wording. Keep the summary under 600 characters. "
            "highlights, actions, and warnings must be arrays of strings. "
            "Do not wrap the JSON in markdown fences.\n\n"
            f"OpenCode output:\n{raw_output[:12000]}"
        )

        payload = {
            "model": runtime["model"],
            "messages": [
                {
                    "role": "system",
                    "content": "You format technical results for Telegram. Return JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }

        content = self._call_chat_completion(runtime, payload)
        if not content:
            return None

        return self._parse_decorator_json(content)

    def _call_chat_completion(self, runtime: dict, payload: dict) -> Optional[str]:
        request = Request(
            url=f"{str(runtime['base_url']).rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {runtime['api_key']}",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=int(runtime["timeout_seconds"])) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("LLM request failed: %s", exc)
            return None

        try:
            response_json = json.loads(response_body)
        except json.JSONDecodeError:
            logger.warning("LLM response was not valid JSON")
            return None

        choices = response_json.get("choices") or []
        if not choices:
            return None

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return None

        message = first_choice.get("message")
        if not isinstance(message, dict):
            return None

        return str(message.get("content") or "").strip()

    def _parse_decorator_json(self, text: str) -> Optional[dict]:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
            if candidate.endswith("```"):
                candidate = candidate[:-3].strip()

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = candidate[start : end + 1]

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None

        if not isinstance(parsed, dict):
            return None

        def as_string_list(value: object) -> List[str]:
            if not isinstance(value, list):
                return []
            items: List[str] = []
            for item in value:
                if item is None:
                    continue
                items.append(str(item))
            return items

        return {
            "title": str(parsed.get("title") or "OpenCode Result"),
            "summary": str(parsed.get("summary") or ""),
            "highlights": as_string_list(parsed.get("highlights")),
            "actions": as_string_list(parsed.get("actions")),
            "warnings": as_string_list(parsed.get("warnings")),
        }

    def _render_decorated_messages(self, payload: dict) -> List[str]:
        messages: List[str] = []

        title = html.escape(str(payload.get("title") or "OpenCode Result"))
        summary = html.escape(str(payload.get("summary") or "").strip())
        if summary:
            messages.append(f"<b>{title}</b>\n{summary}")
        else:
            messages.append(f"<b>{title}</b>")

        def render_section(label: str, items: List[str]) -> Optional[str]:
            cleaned_items = [self._truncate_text(item, 420) for item in items if str(item).strip()]
            if not cleaned_items:
                return None
            lines = [f"<b>{html.escape(label)}</b>"]
            for item in cleaned_items[:6]:
                lines.append(f"• {html.escape(item)}")
            return "\n".join(lines)

        for label, key in (("Highlights", "highlights"), ("Actions", "actions"), ("Warnings", "warnings")):
            rendered = render_section(label, payload.get(key) or [])
            if rendered:
                messages.append(rendered)

        return [message for message in messages if message.strip()]

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        cleaned = str(text).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 1)].rstrip() + "…"

    def get_health_message(self) -> str:
        uptime_seconds = int(time.monotonic() - self._started_at)
        uptime_hours, remainder = divmod(uptime_seconds, 3600)
        uptime_minutes, uptime_seconds = divmod(remainder, 60)
        uptime = f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

        allowed = "any chat" if not self.config.allowed_chat_ids else f"{len(self.config.allowed_chat_ids)} allowed chats"
        decorator_state = "enabled" if self._is_decorated_output_enabled() else "disabled"
        input_llm_state = "enabled" if self._resolve_llm_runtime("input") else "disabled"
        model = self._stats.get("last_model") or self.config.opencode_model or "default"
        last_error = self._stats.get("last_error") or "none"

        lines = [
            "<b>Health</b>",
            f"Status: running",
            f"Uptime: {html.escape(uptime)}",
            f"OpenCode model: {html.escape(str(model))}",
            f"Input LLM rewrite: {html.escape(input_llm_state)}",
            f"Output decoration: {html.escape(decorator_state)}",
            f"Chat access: {html.escape(allowed)}",
            f"Last result: {html.escape(str(self._stats.get('last_result_kind') or 'none'))}",
            f"Last error: {html.escape(str(last_error))}",
        ]
        return "\n".join(lines)

    def get_stats_message(self) -> str:
        uptime_seconds = int(time.monotonic() - self._started_at)
        uptime_hours, remainder = divmod(uptime_seconds, 3600)
        uptime_minutes, uptime_seconds = divmod(remainder, 60)
        uptime = f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

        lines = [
            "<b>Stats</b>",
            f"Requests: {self._stats['requests']}",
            f"Successful: {self._stats['successful_requests']}",
            f"Failed: {self._stats['failed_requests']}",
            f"Quota fallbacks: {self._stats['quota_fallbacks']}",
            f"Prompt rewrites: {self._stats['prompt_rewrites']}",
            f"Input LLM failures: {self._stats['input_llm_failures']}",
            f"Decorated outputs: {self._stats['decorated_outputs']}",
            f"Decorator failures: {self._stats['decorator_failures']}",
            f"Last model: {html.escape(str(self._stats.get('last_model') or 'none'))}",
            f"Uptime: {html.escape(uptime)}", 
        ]
        return "\n".join(lines)

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(
            "Send any text prompt. I will run it through OpenCode and reply with the result."
        )

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(
            "Usage:\n"
            "- Send plain text as a prompt\n"
            "- Optional input LLM rewrites your prompt before OpenCode runs\n"
            "- Optional output LLM prettifies OpenCode result for Telegram\n"
            "- /health shows runtime state\n"
            "- /stats shows request counters\n"
            "- Bot runs: opencode run [--model ...] <your prompt>\n"
            "Config via env vars: TELEGRAM_BOT_TOKEN, OPENCODE_MODEL, OPENCODE_WORKING_DIR"
        )

    async def handle_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(self.get_health_message(), parse_mode="HTML")

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(self.get_stats_message(), parse_mode="HTML")

    def _is_chat_allowed(self, chat_id: int) -> bool:
        if not self.config.allowed_chat_ids:
            return True
        return chat_id in self.config.allowed_chat_ids

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return

        chat_id = update.effective_chat.id
        if not self._is_chat_allowed(chat_id):
            await update.effective_message.reply_text("This chat is not allowed to use this bot.")
            return

        prompt = (update.effective_message.text or "").strip()
        if not prompt:
            await update.effective_message.reply_text("Please send a non-empty prompt.")
            return

        await update.effective_message.reply_text("Request received. Running OpenCode in background...")
        asyncio.create_task(self._run_and_respond(chat_id, prompt, context.application))

    async def _run_and_respond(self, chat_id: int, prompt: str, app: Application) -> None:
        try:
            async with self._semaphore:
                await app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                improved_prompt = await self.enhance_prompt(prompt)
                result = await self.run_prompt(improved_prompt)

            decorated_chunks = await self.decorate_output(result)
            if decorated_chunks:
                for chunk in decorated_chunks:
                    await app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
                return

            for chunk in _chunk_message(result):
                if len(chunk) > TELEGRAM_LIMIT:
                    chunk = chunk[:TELEGRAM_LIMIT]
                await app.bot.send_message(chat_id=chat_id, text=chunk)

        except FileNotFoundError:
            await app.bot.send_message(
                chat_id=chat_id,
                text="opencode binary not found in PATH. Install OpenCode and retry.",
            )
        except Exception as exc:  # broad guard to avoid silent task failures
            logger.exception("Failed to run OpenCode prompt")
            await app.bot.send_message(chat_id=chat_id, text=f"Unexpected error: {exc}")


def configure_logging(log_level: str, log_file: Optional[Path] = None, foreground: bool = True) -> None:
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    handlers: List[logging.Handler] = []
    if foreground:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    if not handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    for handler in handlers:
        root_logger.addHandler(handler)


def build_application(config: BridgeConfig) -> Application:
    bridge = OpenCodeBridge(config)
    app = Application.builder().token(config.telegram_token).build()

    app.add_handler(CommandHandler("start", bridge.handle_start))
    app.add_handler(CommandHandler("help", bridge.handle_help))
    app.add_handler(CommandHandler("health", bridge.handle_health))
    app.add_handler(CommandHandler("stats", bridge.handle_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.handle_text))
    app.add_error_handler(_handle_application_error)
    return app


def run_bridge(config: BridgeConfig, *, foreground: bool = True, log_file: Optional[Path] = None) -> None:
    configure_logging(config.log_level, log_file=log_file, foreground=foreground)
    logger.info("Starting OpenCode Telegram bridge bot")
    app = build_application(config)
    app.run_polling(close_loop=False)


async def _handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        logger.warning("Telegram polling conflict: %s", error)
        return

    logger.error("Telegram application error: %s", error)


def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    config = BridgeConfig.from_env()

    if not Path(config.opencode_working_dir).exists():
        raise ValueError(f"OPENCODE_WORKING_DIR does not exist: {config.opencode_working_dir}")

    run_bridge(config, foreground=True)


if __name__ == "__main__":
    main()
