"""Minimal Telegram -> OpenCode API -> Telegram bridge.

This module provides a sessioned bot flow:
1) receive text in Telegram
2) call OpenCode server API for that chat session
3) send result back to the same Telegram chat
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
import threading
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Set
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from openbridge.llm_service import LLMService
from openbridge.opencode_api_client import OpenCodeAPIClient

logger = logging.getLogger("opencode_bridge")

TELEGRAM_LIMIT = 4096
SAFE_CHUNK = TELEGRAM_LIMIT
DEFAULT_FALLBACK_MODELS = (
    "opencode/minimax-m2.5-free",
    "opencode/nemotron-3-super-free",
)
DEFAULT_DECORATOR_TIMEOUT_SECONDS = 30
DEFAULT_LITELLM_PORT = 8000
DEFAULT_LITELLM_MODEL = "groq-gpt-oss-mini"
DEFAULT_OPENCODE_API_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_OPENCODE_API_TIMEOUT_SECONDS = 120
DEFAULT_CHAT_QUEUE_MAX_PENDING = 5
DEFAULT_CHAT_QUEUE_OVERFLOW_MODE = "reject"
DEFAULT_WORKFLOW_PROMPT_MAX_CHARS = 12000
DEFAULT_WORKFLOW_PROMPT_OVERFLOW_MODE = "reject"
DEFAULT_OPENCODE_BACKOFF_BASE_MS = 250
DEFAULT_OPENCODE_BACKOFF_MAX_MS = 5000
DEFAULT_OPENCODE_BACKOFF_FACTOR = 2.0
DEFAULT_OPENCODE_BACKOFF_JITTER_PCT = 0.2
LEGACY_ENV_PREFIX = "TELEWATCH_"
CURRENT_ENV_PREFIX = "OPENBRIDGE_"
SENSITIVE_LOG_PATTERNS = (
    re.compile(r"(https?://api\.telegram\.org/bot)(\d{6,12}:[A-Za-z0-9_-]+)(/)", re.IGNORECASE),
    re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:sk|gsk|rk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}:[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(
        r"(?i)\b(authorization|api[-_ ]?key|token|password|secret)\b\s*[:=]\s*([\"']?)[^\s\"']+\2"
    ),
)

MDV2_SPECIAL_CHARS = r"_*[]()~`>#+-=|{}.!"
MDV2_LITERAL_SPECIAL_CHARS = r">#+-={}.!"
MDV2_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]*`)")
MDV2_ENTITY_PATTERN = re.compile(
    r"\*[^\*\n]+\*|"
    r"_[^_\n]+_|"
    r"\[[^\]]*\]\([^\)]*\)"
)
MDV2_MAX_FALLBACK_DEPTH = 4
MDV2_STRICT_FALLBACK_THRESHOLD = 400


def _escape_markdown_v2(text: str, *, preserve_formatting: bool = False) -> str:
    text = str(text)
    special_chars = MDV2_SPECIAL_CHARS

    def _escape_chars(raw: str, chars: str = MDV2_SPECIAL_CHARS) -> str:
        escaped: List[str] = []
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "\\":
                if i + 1 < len(raw) and raw[i + 1] in ("n", "\\", *MDV2_SPECIAL_CHARS):
                    escaped.append("\\")
                    escaped.append(raw[i + 1])
                    i += 2
                    continue
                escaped.append("\\\\")
                i += 1
                continue

            if ch in chars:
                escaped.append("\\")
            escaped.append(ch)
            i += 1
        return "".join(escaped)

    def _escape_plain_segment(segment: str) -> str:
        if not preserve_formatting:
            return _escape_chars(segment, special_chars)

        placeholders: dict[str, str] = {}
        protected = segment
        for i, match in enumerate(MDV2_ENTITY_PATTERN.finditer(segment)):
            entity = match.group(0)
            token = f"MDV2ENTITY{i}END"
            if entity.startswith("["):
                parts = entity.split("](", 1)
                if len(parts) == 2:
                    label = parts[0][1:]
                    url_and_close = parts[1]
                    if url_and_close.endswith(")"):
                        url = url_and_close[:-1]
                        escaped_label = _escape_chars(label, special_chars)
                        escaped_url = _escape_chars(url, special_chars)
                        entity = "[" + escaped_label + "](" + escaped_url + ")"
            elif entity.startswith("*") and entity.endswith("*"):
                entity = "*" + _escape_chars(entity[1:-1], special_chars) + "*"
            elif entity.startswith("_") and entity.endswith("_"):
                entity = "_" + _escape_chars(entity[1:-1], special_chars) + "_"
            placeholders[token] = entity
            protected = protected.replace(match.group(0), token, 1)

        output_segment = _escape_chars(protected, special_chars)
        for token, original in placeholders.items():
            output_segment = output_segment.replace(token, original)
        return output_segment

    output: List[str] = []
    last_end = 0
    for match in MDV2_CODE_BLOCK_RE.finditer(text):
        start, end = match.span()
        if start > last_end:
            output.append(_escape_plain_segment(text[last_end:start]))
        output.append(match.group(0))
        last_end = end

    if last_end < len(text):
        output.append(_escape_plain_segment(text[last_end:]))

    return "".join(output)


@dataclass
class BridgeConfig:
    telegram_token: str
    opencode_model: Optional[str]
    opencode_working_dir: str
    opencode_timeout_seconds: int
    max_concurrent_jobs: int
    allowed_chat_ids: Set[int]
    allow_all_chats: bool = False
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
    opencode_api_base_url: str = DEFAULT_OPENCODE_API_BASE_URL
    opencode_api_username: str = "opencode"
    opencode_api_password: Optional[str] = None
    opencode_api_timeout_seconds: int = DEFAULT_OPENCODE_API_TIMEOUT_SECONDS
    opencode_backoff_base_ms: int = DEFAULT_OPENCODE_BACKOFF_BASE_MS
    opencode_backoff_max_ms: int = DEFAULT_OPENCODE_BACKOFF_MAX_MS
    opencode_backoff_factor: float = DEFAULT_OPENCODE_BACKOFF_FACTOR
    opencode_backoff_jitter_pct: float = DEFAULT_OPENCODE_BACKOFF_JITTER_PCT
    chat_queue_max_pending: int = DEFAULT_CHAT_QUEUE_MAX_PENDING
    chat_queue_overflow_mode: str = DEFAULT_CHAT_QUEUE_OVERFLOW_MODE
    workflow_prompt_max_chars: int = DEFAULT_WORKFLOW_PROMPT_MAX_CHARS
    workflow_prompt_overflow_mode: str = DEFAULT_WORKFLOW_PROMPT_OVERFLOW_MODE

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, str]) -> "BridgeConfig":
        mapping = _with_legacy_openbridge_aliases(mapping)
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

        allow_all_chats = _parse_bool(mapping.get("TELEGRAM_ALLOW_ALL_CHATS", "0"))
        if allow_all_chats and allowed_chat_ids:
            logger.warning("TELEGRAM_ALLOW_ALL_CHATS is enabled, ignoring TELEGRAM_ALLOWED_CHAT_IDS")
        elif not allow_all_chats and not allowed_chat_ids:
            logger.warning(
                "TELEGRAM_ALLOWED_CHAT_IDS is empty; the bot will reject all chats unless TELEGRAM_ALLOW_ALL_CHATS is set"
            )

        timeout = int(mapping.get("OPENCODE_TIMEOUT_SECONDS", "600"))
        max_jobs = int(mapping.get("OPENCODE_MAX_CONCURRENT", "1"))
        if timeout <= 0:
            raise ValueError("OPENCODE_TIMEOUT_SECONDS must be > 0")
        if max_jobs <= 0:
            raise ValueError("OPENCODE_MAX_CONCURRENT must be > 0")

        opencode_api_base_url = (
            mapping.get("OPENCODE_API_BASE_URL", DEFAULT_OPENCODE_API_BASE_URL).strip()
            or DEFAULT_OPENCODE_API_BASE_URL
        )
        opencode_api_username = mapping.get("OPENCODE_API_USERNAME", "opencode").strip() or "opencode"
        opencode_api_password = mapping.get("OPENCODE_API_PASSWORD", "").strip() or None
        opencode_api_timeout_seconds = int(
            mapping.get("OPENCODE_API_TIMEOUT_SECONDS", str(DEFAULT_OPENCODE_API_TIMEOUT_SECONDS))
        )
        if opencode_api_timeout_seconds <= 0:
            raise ValueError("OPENCODE_API_TIMEOUT_SECONDS must be > 0")

        opencode_backoff_base_ms = int(
            mapping.get("OPENBRIDGE_OPENCODE_BACKOFF_BASE_MS", str(DEFAULT_OPENCODE_BACKOFF_BASE_MS))
        )
        if opencode_backoff_base_ms <= 0:
            raise ValueError("OPENBRIDGE_OPENCODE_BACKOFF_BASE_MS must be > 0")

        opencode_backoff_max_ms = int(
            mapping.get("OPENBRIDGE_OPENCODE_BACKOFF_MAX_MS", str(DEFAULT_OPENCODE_BACKOFF_MAX_MS))
        )
        if opencode_backoff_max_ms <= 0:
            raise ValueError("OPENBRIDGE_OPENCODE_BACKOFF_MAX_MS must be > 0")

        opencode_backoff_factor = float(
            mapping.get("OPENBRIDGE_OPENCODE_BACKOFF_FACTOR", str(DEFAULT_OPENCODE_BACKOFF_FACTOR))
        )
        if opencode_backoff_factor <= 1.0:
            raise ValueError("OPENBRIDGE_OPENCODE_BACKOFF_FACTOR must be > 1.0")

        opencode_backoff_jitter_pct = float(
            mapping.get("OPENBRIDGE_OPENCODE_BACKOFF_JITTER_PCT", str(DEFAULT_OPENCODE_BACKOFF_JITTER_PCT))
        )
        if not (0.0 <= opencode_backoff_jitter_pct <= 1.0):
            raise ValueError("OPENBRIDGE_OPENCODE_BACKOFF_JITTER_PCT must be between 0.0 and 1.0")

        chat_queue_max_pending = int(
            mapping.get("OPENBRIDGE_CHAT_QUEUE_MAX_PENDING", str(DEFAULT_CHAT_QUEUE_MAX_PENDING))
        )
        if chat_queue_max_pending <= 0:
            raise ValueError("OPENBRIDGE_CHAT_QUEUE_MAX_PENDING must be > 0")

        chat_queue_overflow_mode = (
            mapping.get("OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE", DEFAULT_CHAT_QUEUE_OVERFLOW_MODE)
            .strip()
            .lower()
            or DEFAULT_CHAT_QUEUE_OVERFLOW_MODE
        )
        if chat_queue_overflow_mode not in {"reject", "drop_oldest"}:
            raise ValueError("OPENBRIDGE_CHAT_QUEUE_OVERFLOW_MODE must be 'reject' or 'drop_oldest'")

        workflow_prompt_max_chars = int(
            mapping.get("OPENBRIDGE_WORKFLOW_PROMPT_MAX_CHARS", str(DEFAULT_WORKFLOW_PROMPT_MAX_CHARS))
        )
        if workflow_prompt_max_chars <= 0:
            raise ValueError("OPENBRIDGE_WORKFLOW_PROMPT_MAX_CHARS must be > 0")

        workflow_prompt_overflow_mode = (
            mapping.get("OPENBRIDGE_WORKFLOW_PROMPT_OVERFLOW_MODE", DEFAULT_WORKFLOW_PROMPT_OVERFLOW_MODE)
            .strip()
            .lower()
            or DEFAULT_WORKFLOW_PROMPT_OVERFLOW_MODE
        )
        if workflow_prompt_overflow_mode not in {"reject", "truncate"}:
            raise ValueError("OPENBRIDGE_WORKFLOW_PROMPT_OVERFLOW_MODE must be 'reject' or 'truncate'")

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
        ) = _parse_llm_role_config(mapping, role="OPENBRIDGE_INPUT_LLM")

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
            role="OPENBRIDGE_OUTPUT_LLM",
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
            allow_all_chats=allow_all_chats,
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
            opencode_api_base_url=opencode_api_base_url,
            opencode_api_username=opencode_api_username,
            opencode_api_password=opencode_api_password,
            opencode_api_timeout_seconds=opencode_api_timeout_seconds,
            opencode_backoff_base_ms=opencode_backoff_base_ms,
            opencode_backoff_max_ms=opencode_backoff_max_ms,
            opencode_backoff_factor=opencode_backoff_factor,
            opencode_backoff_jitter_pct=opencode_backoff_jitter_pct,
            chat_queue_max_pending=chat_queue_max_pending,
            chat_queue_overflow_mode=chat_queue_overflow_mode,
            workflow_prompt_max_chars=workflow_prompt_max_chars,
            workflow_prompt_overflow_mode=workflow_prompt_overflow_mode,
        )

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls.from_mapping(os.environ)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _with_legacy_openbridge_aliases(mapping: Mapping[str, str]) -> dict[str, str]:
    normalized = dict(mapping)
    for key, value in mapping.items():
        if not key.startswith(LEGACY_ENV_PREFIX):
            continue

        suffix = key[len(LEGACY_ENV_PREFIX) :]
        current_key = f"{CURRENT_ENV_PREFIX}{suffix}"
        current_value = str(normalized.get(current_key, "")).strip()
        if current_value:
            continue

        normalized[current_key] = value
    return normalized


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
    decorator_api_key = mapping.get("OPENBRIDGE_DECORATOR_API_KEY", "").strip() or None
    decorator_model = mapping.get("OPENBRIDGE_DECORATOR_MODEL", "").strip() or None
    decorator_base_url = mapping.get("OPENBRIDGE_DECORATOR_BASE_URL", "").strip() or None
    decorator_timeout_seconds = int(
        mapping.get("OPENBRIDGE_DECORATOR_TIMEOUT_SECONDS", str(DEFAULT_DECORATOR_TIMEOUT_SECONDS))
    )
    decorator_enabled = _parse_bool(mapping.get("OPENBRIDGE_DECORATOR_ENABLED", "0"))
    if decorator_api_key and decorator_model and decorator_base_url:
        decorator_enabled = True
    if decorator_enabled and (not decorator_api_key or not decorator_model or not decorator_base_url):
        decorator_enabled = False
    if decorator_timeout_seconds <= 0:
        raise ValueError("OPENBRIDGE_DECORATOR_TIMEOUT_SECONDS must be > 0")
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

    if role == "OPENBRIDGE_OUTPUT_LLM":
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
    def _replace(match: re.Match[str]) -> str:
        if match.lastindex and match.lastindex >= 3:
            return f"{match.group(1)}[REDACTED]{match.group(3)}"
        if match.lastindex and match.lastindex >= 1:
            return f"{match.group(1)}=[REDACTED]"
        return "[REDACTED]"

    redacted = text
    for pattern in SENSITIVE_LOG_PATTERNS:
        redacted = pattern.sub(_replace, redacted)
    return redacted


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _redact_sensitive_text(rendered)


def _chunk_message(text: str, limit: int = SAFE_CHUNK) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return

    start = 0
    while start < len(text):
        remaining = text[start:]
        if len(remaining) <= limit:
            yield remaining if remaining.strip() else "(empty)"
            return

        split = _find_section_split_index(remaining, limit)
        if split <= 0:
            split = _find_markdown_safe_split_index(remaining, limit)
        if split <= 0 or split >= len(remaining):
            split = limit

        chunk = remaining[:split]
        yield chunk if chunk.strip() else "(empty)"
        start += split


def _find_markdown_safe_split_index(text: str, target: int) -> int:
    if target <= 0 or target >= len(text):
        return min(max(target, 0), len(text))

    inside_fence = False
    safe_before_target = -1
    safe_after_target = -1
    index = 0

    for line in text.splitlines(keepends=True):
        line_end = index + len(line)
        stripped = line.strip()
        if stripped.startswith("```"):
            inside_fence = not inside_fence

        if not inside_fence:
            if line_end <= target:
                safe_before_target = line_end
            elif safe_after_target == -1:
                safe_after_target = line_end

        index = line_end

    if safe_before_target != -1:
        return safe_before_target
    if safe_after_target != -1 and safe_after_target < len(text):
        return safe_after_target
    return target


def _find_section_split_index(text: str, target: int) -> int:
    if target <= 0 or target >= len(text):
        return min(max(target, 0), len(text))

    candidates: List[int] = []
    for marker in ("\n\n*", "\n\n•", "\n\n- ", "\n\n"):
        index = text.rfind(marker, 0, target)
        if index > 100:
            candidates.append(index + 2)

    if not candidates:
        return -1

    split = max(candidates)
    # Do not split in the middle of a fenced code block.
    if text[:split].count("```") % 2 != 0:
        return -1
    return split


def _extract_session_id(payload: object) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("id", "sessionId", "session_id"):
            value = payload.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)

        for nested_key in ("data", "result", "session"):
            nested = payload.get(nested_key)
            value = _extract_session_id(nested)
            if value:
                return value

    if isinstance(payload, list):
        for item in payload:
            value = _extract_session_id(item)
            if value:
                return value
    return None


def _extract_text_candidates(payload: object) -> List[str]:
    candidates: List[str] = []

    if isinstance(payload, str):
        text = payload.strip()
        if text:
            candidates.append(text)
        return candidates

    if isinstance(payload, dict):
        # Handle part payloads explicitly (common in OpenCode API messages).
        part_type = str(payload.get("type") or "").lower()
        if part_type in {"text", "input_text", "output_text"}:
            for text_key in ("text", "content", "value"):
                if text_key in payload:
                    candidates.extend(_extract_text_candidates(payload.get(text_key)))

        # Prefer assistant-like roles if available.
        role = str(payload.get("role") or payload.get("type") or "").lower()
        if role in {"assistant", "ai", "response"}:
            for key in ("content", "text", "message", "output", "response"):
                if key in payload:
                    candidates.extend(_extract_text_candidates(payload.get(key)))

        for key in (
            "content",
            "text",
            "message",
            "output",
            "response",
            "messages",
            "items",
            "parts",
            "data",
            "result",
            "choices",
        ):
            if key in payload:
                candidates.extend(_extract_text_candidates(payload.get(key)))
        return candidates

    if isinstance(payload, list):
        for item in payload:
            candidates.extend(_extract_text_candidates(item))

    return [item for item in candidates if item.strip()]


class OpenCodeBridge:
    def __init__(self, config: BridgeConfig):
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
        self._started_at = time.monotonic()
        self._stats = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
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
        self._chat_sessions: dict[int, str] = {}
        self._session_lock = asyncio.Lock()
        self._workflow_stats_provider: Optional[Callable[[], List[str]]] = None
        self._workflow_manager: Any = None
        self._workflow_file_lock = asyncio.Lock()
        self._pending_workflow_drafts: dict[int, dict] = {}
        self._chat_queue_lock = asyncio.Lock()
        self._chat_queues: dict[int, asyncio.Queue[tuple[str, Application]]] = {}
        self._chat_workers: dict[int, asyncio.Task[Any]] = {}

        # Initialize service instances
        self._api_client = OpenCodeAPIClient(
            api_base_url=config.opencode_api_base_url,
            api_username=config.opencode_api_username,
            api_password=config.opencode_api_password,
            api_timeout_seconds=config.opencode_api_timeout_seconds,
            backoff_base_ms=config.opencode_backoff_base_ms,
            backoff_max_ms=config.opencode_backoff_max_ms,
            backoff_factor=config.opencode_backoff_factor,
            backoff_jitter_pct=config.opencode_backoff_jitter_pct,
        )

        self._llm_service = LLMService(resolve_runtime=self._resolve_llm_runtime)

    async def close(self) -> None:
        async with self._chat_queue_lock:
            workers = list(self._chat_workers.values())
            self._chat_workers.clear()
            self._chat_queues.clear()
        async with self._session_lock:
            self._chat_sessions.clear()
        async with self._workflow_file_lock:
            self._pending_workflow_drafts.clear()
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
        logger.info("OpenCode bridge state cleared during shutdown")

    def set_workflow_stats_provider(self, provider: Optional[Callable[[], List[str]]]) -> None:
        self._workflow_stats_provider = provider

    def set_workflow_manager(self, manager: Any) -> None:
        self._workflow_manager = manager

    async def run_prompt(self, chat_id: int, prompt: str) -> str:
        self._stats["requests"] += 1
        self._stats["last_request_at"] = time.time()
        try:
            session_id = await self._get_or_create_session(chat_id)
        except Exception as exc:
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = str(exc)
            self._stats["last_result_kind"] = "session-error"
            logger.exception("OpenCode session creation failed for chat %s", chat_id)
            return "OpenCode API session error. Check logs for details."

        try:
            result = await asyncio.to_thread(self._run_prompt_via_api_sync, session_id, prompt)
        except Exception as exc:
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = str(exc)
            self._stats["last_result_kind"] = "api-error"
            logger.exception("OpenCode API request failed for chat %s", chat_id)
            return "OpenCode API request failed. Check logs for details."

        self._stats["last_model"] = self.config.opencode_model or "default"
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

    async def _get_or_create_session(self, chat_id: int) -> str:
        existing = self._chat_sessions.get(chat_id)
        if existing:
            return existing

        async with self._session_lock:
            existing = self._chat_sessions.get(chat_id)
            if existing:
                return existing

            session_id = await asyncio.to_thread(self._create_session_sync)
            self._chat_sessions[chat_id] = session_id
            return session_id

    def _create_session_sync(self) -> str:
        payload = self._opencode_request_sync("POST", "/session", payload={})
        session_id = _extract_session_id(payload)
        if not session_id:
            raise RuntimeError("OpenCode API did not return a session id")
        return session_id

    def _run_prompt_via_api_sync(self, session_id: str, prompt: str) -> str:
        return self._api_client.run_prompt_with_polling(
            session_id,
            prompt,
            self.config.opencode_timeout_seconds,
        )

    def _send_session_message_sync(self, session_id: str, prompt: str) -> Optional[str]:
        encoded_session = quote(session_id, safe="")
        # OpenCode currently expects message parts with a typed text object.
        # Avoid broad fallback payloads that can mask the real upstream error.
        payload_variants = [
            {"parts": [{"type": "text", "text": prompt}]},
        ]

        first_error: Optional[Exception] = None
        for payload in payload_variants:
            try:
                response = self._opencode_request_sync(
                    "POST",
                    f"/session/{encoded_session}/message",
                    payload=payload,
                )
                candidates = _extract_text_candidates(response)
                if candidates:
                    return candidates[-1]
                return None
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                # Timeout should surface immediately; retrying with a different
                # payload shape turns a timeout into misleading schema errors.
                if "timeout" in str(exc).lower():
                    raise exc

        if first_error is not None:
            raise first_error
        return None

    def _fetch_session_messages_sync(self, session_id: str) -> object:
        encoded_session = quote(session_id, safe="")
        return self._opencode_request_sync("GET", f"/session/{encoded_session}/message")

    def _opencode_request_sync(self, method: str, path: str, payload: Optional[dict] = None) -> object:
        base_url = self.config.opencode_api_base_url.rstrip("/")
        url = f"{base_url}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.config.opencode_api_password:
            raw = f"{self.config.opencode_api_username}:{self.config.opencode_api_password}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.config.opencode_api_timeout_seconds) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except (UnicodeDecodeError, IOError):
                detail = str(exc)
            raise RuntimeError(f"OpenCode API HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"OpenCode API request error: {exc}") from exc

        if not response_body.strip():
            return {}

        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            logger.debug("OpenCode response was not valid JSON, treating as text: %s", exc)
            return {"text": response_body}

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
        return self._llm_service._enhance_prompt_sync(runtime, raw_prompt)

    def _is_error_result(self, text: str) -> bool:
        error_prefixes = (
            "OpenCode API timed out",
            "OpenCode API HTTP",
            "OpenCode API request failed",
            "OpenCode API request error",
            "OpenCode API session error",
            "OpenCode failed",
            "OpenCode could not use",
            "OpenCode rejected",
            "OpenCode API did not return a session id",
            "OpenCode returned no output.",
        )
        return text.startswith(error_prefixes)

    def _is_decorated_output_enabled(self) -> bool:
        return self._resolve_llm_runtime("output") is not None

    async def decorate_output(self, raw_output: str) -> Optional[List[str]]:
        if self._is_error_result(raw_output):
            return None

        if not self._is_decorated_output_enabled():
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
        return self._llm_service._decorate_output_sync(raw_output, runtime)

    def _call_chat_completion(self, runtime: dict, payload: dict) -> Optional[str]:
        return self._llm_service._call_chat_completion(runtime, payload)

    def _parse_decorator_json(self, text: str) -> Optional[dict]:
        return self._llm_service._parse_decorator_json(text)

    def _render_decorated_messages(self, payload: dict) -> List[str]:
        messages: List[str] = []

        title = _escape_markdown_v2(str(payload.get("title") or "OpenCode Result"))
        summary = _escape_markdown_v2(str(payload.get("summary") or "").strip())
        if summary:
            messages.append(f"*{title}*\n{summary}")
        else:
            messages.append(f"*{title}*")

        def render_section(label: str, items: List[str]) -> Optional[str]:
            cleaned_items = [self._truncate_text(item, 420) for item in items if str(item).strip()]
            if not cleaned_items:
                return None
            lines = [f"*{_escape_markdown_v2(label)}*"]
            for item in cleaned_items[:6]:
                lines.append(f"• {_escape_markdown_v2(item)}")
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
            "*Health*",
            f"Status: running",
            f"Uptime: {_escape_markdown_v2(uptime)}",
            f"OpenCode model: {_escape_markdown_v2(str(model))}",
            f"OpenCode API: {_escape_markdown_v2(self.config.opencode_api_base_url)}",
            f"Active sessions: {len(self._chat_sessions)}",
            f"Input LLM rewrite: {_escape_markdown_v2(input_llm_state)}",
            f"Output decoration: {_escape_markdown_v2(decorator_state)}",
            f"Chat access: {_escape_markdown_v2(allowed)}",
            f"Last result: {_escape_markdown_v2(str(self._stats.get('last_result_kind') or 'none'))}",
            f"Last error: {_escape_markdown_v2(str(last_error))}",
        ]
        return "\n".join(lines)

    def get_stats_message(self) -> str:
        uptime_seconds = int(time.monotonic() - self._started_at)
        uptime_hours, remainder = divmod(uptime_seconds, 3600)
        uptime_minutes, uptime_seconds = divmod(remainder, 60)
        uptime = f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

        lines = [
            "*Stats*",
            f"Requests: {self._stats['requests']}",
            f"Successful: {self._stats['successful_requests']}",
            f"Failed: {self._stats['failed_requests']}",
            f"Prompt rewrites: {self._stats['prompt_rewrites']}",
            f"Input LLM failures: {self._stats['input_llm_failures']}",
            f"Decorated outputs: {self._stats['decorated_outputs']}",
            f"Decorator failures: {self._stats['decorator_failures']}",
            f"Last model: {_escape_markdown_v2(str(self._stats.get('last_model') or 'none'))}",
            f"Uptime: {_escape_markdown_v2(uptime)}",
            f"Pending workflow drafts: {len(self._pending_workflow_drafts)}",
        ]
        if self._workflow_stats_provider is not None:
            try:
                workflow_lines = self._workflow_stats_provider()
            except (RuntimeError, ValueError, TypeError) as exc:
                logger.warning("Workflow stats provider failed: %s", exc)
                workflow_lines = [f"Workflows stats error: {exc}"]
            except Exception as exc:
                # Top-level guard: unexpected errors should not crash stats reporting
                logger.error("Unexpected error in workflow stats provider: %s", exc)
                workflow_lines = ["Workflows stats unavailable"]
            if workflow_lines:
                lines.append("")
                lines.append("*Workflows*")
                lines.extend(_escape_markdown_v2(str(item)) for item in workflow_lines)
        return "\n".join(lines)

    @staticmethod
    def _slugify_workflow_id(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
        return slug or "workflow"

    @staticmethod
    def _extract_json_object_text(text: str) -> Optional[str]:
        try:
            candidate = text.strip()
            if candidate.startswith("```"):
                first_newline = candidate.find("\n")
                if first_newline != -1:
                    candidate = candidate[first_newline + 1 :]
                if candidate.endswith("```"):
                    candidate = candidate[:-3]
                candidate = candidate.strip()

            start = candidate.find("{")
            if start == -1:
                return None

            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                ch = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue

                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return candidate[start : index + 1]
            return None
        except (AttributeError, IndexError, TypeError) as exc:
            logger.debug("JSON object extraction failed: %s", exc)
            return None

    @staticmethod
    def _coerce_single_workflow(payload: object) -> dict:
        if isinstance(payload, dict):
            if isinstance(payload.get("workflows"), list) and payload["workflows"]:
                first = payload["workflows"][0]
                if isinstance(first, dict):
                    return dict(first)
            return dict(payload)
        raise ValueError("Workflow draft must be a JSON object")

    @staticmethod
    def _workflow_file_path() -> Path:
        from .workflows import DEFAULT_WORKFLOWS_FILE

        return DEFAULT_WORKFLOWS_FILE

    async def _draft_workflow_from_instruction(
        self,
        *,
        chat_id: int,
        instruction: str,
        existing_draft: Optional[dict] = None,
    ) -> dict:
        from .workflows import WorkflowDefinition, WorkflowState, _next_run_timestamp

        existing_text = ""
        if existing_draft is not None:
            existing_text = "\n\nExisting workflow draft JSON:\n" + json.dumps(existing_draft, indent=2)

        authoring_prompt = (
            "Convert the user's natural-language request into ONE workflow JSON object for OpenBridge. "
            "Return JSON only with no markdown fences and no commentary.\n\n"
            "Required top-level fields:\n"
            "- id (snake_case)\n"
            "- name\n"
            "- enabled (boolean)\n"
            "- timezone (\"local\" or \"UTC\")\n"
            "- schedule (daily@HH:MM OR every:<seconds> OR cron:<5 fields>)\n"
            "- targets (array of numeric chat ids)\n"
            "- steps (array)\n\n"
            "Allowed step types: http_fetch, transform_python, opencode_prompt, telegram_send.\n"
            "For news workflows, use http_fetch normalize=\"rss_digest\" and include max_items.\n"
            "For Gmail/Calendar/Drive-style workflows in this phase, do NOT use mcp_tool_call. "
            "Instead, use opencode_prompt and instruct OpenCode to call MCP tools internally.\n"
            "When the user mentions a specific MCP profile like gws-arindam or gws-kiit, embed that profile name "
            "clearly in the opencode_prompt instructions.\n"
            "Always include telegram_send as the final step.\n"
            "Use chat target "
            f"{chat_id} if target is unspecified.\n"
            "Keep prompt_template concise and practical.\n"
            "Example Gmail digest workflow shape for this phase:\n"
            "{\n"
            "  \"id\": \"personal_gmail_digest\",\n"
            "  \"name\": \"Personal Gmail Digest\",\n"
            "  \"enabled\": true,\n"
            "  \"timezone\": \"local\",\n"
            "  \"schedule\": \"cron:0 9 * * *\",\n"
            "  \"targets\": [CHAT_ID],\n"
            "  \"steps\": [\n"
            "    {\n"
            "      \"type\": \"opencode_prompt\",\n"
            "      \"prompt_template\": \"Using MCP server gws-arindam, fetch top 10 important emails from the last day and create a concise digest with sender, subject, and why it matters.\"\n"
            "    },\n"
            "    {\"type\": \"telegram_send\"}\n"
            "  ]\n"
            "}\n"
            f"\nUser request:\n{instruction}{existing_text}"
        )

        authoring_chat_id = -2_000_000_000 - abs(chat_id)
        draft_text = await self.run_prompt(authoring_chat_id, authoring_prompt)
        if self._is_error_result(draft_text):
            raise ValueError(draft_text)

        json_text = self._extract_json_object_text(draft_text)
        if not json_text:
            raise ValueError("Could not extract workflow JSON from model output")

        parsed = json.loads(json_text)
        workflow_obj = self._coerce_single_workflow(parsed)

        safety_errors = self._validate_workflow_safety(workflow_obj, chat_id)
        if safety_errors:
            raise ValueError("Workflow safety validation failed: " + "; ".join(safety_errors))

        if not workflow_obj.get("name"):
            workflow_obj["name"] = "Telegram Workflow"
        if not workflow_obj.get("id"):
            workflow_obj["id"] = self._slugify_workflow_id(str(workflow_obj.get("name", "workflow")))
        if "enabled" not in workflow_obj:
            workflow_obj["enabled"] = True
        if not workflow_obj.get("timezone"):
            workflow_obj["timezone"] = "local"
        if not workflow_obj.get("targets"):
            workflow_obj["targets"] = [chat_id]

        validated = WorkflowDefinition.from_mapping(workflow_obj)
        _ = _next_run_timestamp(validated, WorkflowState(), time.time())

        return {
            "id": validated.id,
            "name": validated.name,
            "enabled": validated.enabled,
            "timezone": validated.timezone,
            "schedule": validated.schedule,
            "targets": validated.targets,
            "steps": [{"type": step.type, **step.params} for step in validated.steps],
            "retry_policy": validated.retry_policy,
            "dedupe_policy": validated.dedupe_policy,
            "metadata": validated.metadata,
        }

    @staticmethod
    def _validate_workflow_safety(workflow_obj: dict, chat_id: int) -> List[str]:
        errors: List[str] = []

        steps = workflow_obj.get("steps", [])
        if not isinstance(steps, list) or not steps:
            errors.append("workflow must contain at least one step")
            return errors

        if len(steps) > 10:
            errors.append("workflow cannot contain more than 10 steps")

        allowed_types = {"http_fetch", "transform_python", "opencode_prompt", "telegram_send"}
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(f"step {index} must be an object")
                continue

            step_type = str(step.get("type", "")).strip().lower()
            if step_type not in allowed_types:
                errors.append(f"step {index} has unsupported type '{step_type}'")
                continue

            if step_type == "http_fetch":
                sources = step.get("sources", [])
                if not isinstance(sources, list) or not sources:
                    errors.append(f"step {index} must include a non-empty sources list")
                elif len(sources) > 5:
                    errors.append(f"step {index} cannot fetch more than 5 sources")

            if step_type == "opencode_prompt":
                prompt_template = str(step.get("prompt_template") or step.get("prompt") or "")
                if len(prompt_template) > 5000:
                    errors.append(f"step {index} prompt template is too large")

            if step_type == "telegram_send":
                targets = step.get("targets")
                if targets is not None and not isinstance(targets, list):
                    errors.append(f"step {index} targets must be a list if provided")

        targets = workflow_obj.get("targets", [])
        if not isinstance(targets, list) or not targets:
            errors.append("workflow must target at least one chat")
        else:
            for target in targets:
                try:
                    target_id = int(target)
                except (TypeError, ValueError):
                    errors.append(f"invalid target chat id: {target}")
                    continue
                if target_id != chat_id:
                    errors.append("workflows created from chat must target the requesting chat only")
                    break

        schedule = str(workflow_obj.get("schedule", "")).strip()
        if not schedule:
            errors.append("workflow schedule is missing")

        return errors

    def _format_workflow_preview(self, workflow_def: dict) -> str:
        from .workflows import WorkflowDefinition, WorkflowState, _format_timestamp, _next_run_timestamp

        validated = WorkflowDefinition.from_mapping(workflow_def)
        next_run = _next_run_timestamp(validated, WorkflowState(), time.time())
        step_names = [step.type for step in validated.steps]
        return (
            "Workflow draft ready:\n"
            f"- id: {validated.id}\n"
            f"- name: {validated.name}\n"
            f"- schedule: {validated.schedule}\n"
            f"- timezone: {validated.timezone}\n"
            f"- targets: {validated.targets}\n"
            f"- steps: {step_names}\n"
            f"- next run: {_format_timestamp(next_run)}\n\n"
            "Reply with one of:\n"
            "- YES (save)\n"
            "- RUN (save and run now)\n"
            "- EDIT <changes> (revise draft)\n"
            "- CANCEL (discard)"
        )

    async def _save_workflow_definition(self, workflow_def: dict) -> tuple[Path, bool]:
        from .workflows import save_workflows

        workflows_file = self._workflow_file_path()
        async with self._workflow_file_lock:
            existing_items: List[dict] = []
            if workflows_file.exists():
                try:
                    raw = json.loads(workflows_file.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and isinstance(raw.get("workflows"), list):
                        existing_items = [item for item in raw["workflows"] if isinstance(item, dict)]
                    elif isinstance(raw, list):
                        existing_items = [item for item in raw if isinstance(item, dict)]
                except json.JSONDecodeError:
                    existing_items = []

            replaced = False
            merged: List[dict] = []
            for item in existing_items:
                if str(item.get("id", "")).strip() == str(workflow_def.get("id", "")).strip():
                    merged.append(dict(workflow_def))
                    replaced = True
                else:
                    merged.append(item)
            if not replaced:
                merged.append(dict(workflow_def))

            save_workflows(workflows_file, {"workflows": merged})
            return workflows_file, replaced

    async def _run_workflow_now(self, workflow_id: str, app: Application) -> str:
        if self._workflow_manager is None:
            return "Workflow saved, but no active workflow manager was attached."

        result = await self._workflow_manager.run_workflow(workflow_id, telegram_bot=app.bot, manual=True)
        if result.status == "success":
            return f"Workflow {workflow_id} executed successfully in {result.duration_seconds:.2f}s."
        if result.status == "skipped":
            return f"Workflow {workflow_id} skipped: {result.skipped_reason}"
        logger.error("Workflow %s failed: %s", workflow_id, result.error)
        return f"Workflow {workflow_id} failed. Check logs for details."

    async def _handle_pending_workflow_reply(self, chat_id: int, prompt: str, app: Any) -> Optional[str]:
        pending = self._pending_workflow_drafts.get(chat_id)
        if not pending:
            return None

        raw = prompt.strip()
        decision = raw.upper()
        if decision == "CANCEL":
            self._pending_workflow_drafts.pop(chat_id, None)
            return "Workflow draft discarded."

        if decision == "YES" or decision == "RUN":
            workflow_def = pending["workflow"]
            workflows_file, replaced = await self._save_workflow_definition(workflow_def)
            self._pending_workflow_drafts.pop(chat_id, None)
            action_text = "updated" if replaced else "saved"
            message = f"Workflow {workflow_def['id']} {action_text} in {workflows_file}."
            if decision == "RUN":
                run_message = await self._run_workflow_now(str(workflow_def["id"]), app)
                return f"{message}\n{run_message}"
            return message

        if decision.startswith("EDIT"):
            delta = raw[4:].strip()
            if not delta:
                return "Use EDIT <changes> to revise the draft, for example: EDIT run at 07:30 and use 8 items."

            revised = await self._draft_workflow_from_instruction(
                chat_id=chat_id,
                instruction=delta,
                existing_draft=pending["workflow"],
            )
            pending["workflow"] = revised
            return self._format_workflow_preview(revised)

        return "You have a pending workflow draft. Reply with YES, RUN, EDIT <changes>, or CANCEL."

    async def handle_workflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            if not update.effective_message or not update.effective_chat:
                logger.warning("handle_workflow_command called with missing message or chat")
                return

            chat_id = update.effective_chat.id
            if not self._is_chat_allowed(chat_id):
                try:
                    await update.effective_message.reply_text("This chat is not allowed to manage workflows.")
                except Exception as reply_exc:
                    logger.error("Failed to send workflow access denial to chat %s: %s", chat_id, reply_exc)
                return

            args = list(context.args or [])
            if not args:
                try:
                    await update.effective_message.reply_text(
                        "Workflow commands:\n"
                        "/workflow create <natural language request>\n"
                        "/workflow list\n"
                        "/workflow status <id>\n"
                        "/workflow pause <id>\n"
                        "/workflow resume <id>\n"
                        "/workflow run <id>"
                    )
                except Exception as reply_exc:
                    logger.error("Failed to send workflow help to chat %s: %s", chat_id, reply_exc)
                return

            action = args[0].strip().lower()
            if action == "create":
                instruction = " ".join(args[1:]).strip()
                if not instruction:
                    try:
                        await update.effective_message.reply_text("Usage: /workflow create <natural language request>")
                    except Exception as reply_exc:
                        logger.error("Failed to send usage message to chat %s: %s", chat_id, reply_exc)
                    return
                try:
                    draft = await self._draft_workflow_from_instruction(chat_id=chat_id, instruction=instruction)
                except Exception as exc:
                    logger.exception("Workflow draft generation failed for chat %s", chat_id)
                    try:
                        await update.effective_message.reply_text("Could not draft workflow. Check logs for details.")
                    except Exception as reply_exc:
                        logger.error("Failed to send workflow draft error to chat %s: %s", chat_id, reply_exc)
                    return

                try:
                    self._pending_workflow_drafts[chat_id] = {"workflow": draft, "source": instruction}
                    await update.effective_message.reply_text(self._format_workflow_preview(draft))
                except Exception as exc:
                    logger.exception("Error handling workflow draft for chat %s", chat_id)
                    try:
                        await update.effective_message.reply_text("Failed to process workflow draft. Check logs.")
                    except Exception as reply_exc:
                        logger.error("Failed to notify workflow draft error to chat %s: %s", chat_id, reply_exc)
                return

            if action == "list":
                if self._workflow_manager is not None:
                    await update.effective_message.reply_text(self._workflow_manager.summary_text())
                    return

                from .workflows import load_workflows

                workflows = load_workflows(self._workflow_file_path())
                if not workflows:
                    await update.effective_message.reply_text("No workflows configured.")
                    return
                items = [f"- {item.id}: {item.name} ({item.schedule})" for item in workflows]
                await update.effective_message.reply_text("Configured workflows:\n" + "\n".join(items))
                return

            if len(args) < 2:
                await update.effective_message.reply_text("This action requires a workflow id.")
                return

            workflow_id = args[1].strip()
            if action == "status":
                if self._workflow_manager is None:
                    await update.effective_message.reply_text("Workflow manager is not attached.")
                    return
                try:
                    text = self._workflow_manager.status_text(workflow_id)
                except Exception:
                    logger.exception("Failed to fetch workflow status for %s", workflow_id)
                    await update.effective_message.reply_text("Could not fetch workflow status. Check logs for details.")
                    return
                await update.effective_message.reply_text(text)
                return

            if action == "pause":
                if self._workflow_manager is None:
                    await update.effective_message.reply_text("Workflow manager is not attached.")
                    return
                try:
                    self._workflow_manager.set_paused(workflow_id, True)
                except Exception:
                    logger.exception("Failed to pause workflow %s", workflow_id)
                    await update.effective_message.reply_text("Could not pause workflow. Check logs for details.")
                    return
                await update.effective_message.reply_text(f"Paused workflow: {workflow_id}")
                return

            if action == "resume":
                if self._workflow_manager is None:
                    await update.effective_message.reply_text("Workflow manager is not attached.")
                    return
                try:
                    self._workflow_manager.set_paused(workflow_id, False)
                except Exception:
                    logger.exception("Failed to resume workflow %s", workflow_id)
                    await update.effective_message.reply_text("Could not resume workflow. Check logs for details.")
                    return
                await update.effective_message.reply_text(f"Resumed workflow: {workflow_id}")
                return

            if action == "run":
                message = await self._run_workflow_now(workflow_id, context.application)
                await update.effective_message.reply_text(message)
                return

            await update.effective_message.reply_text(f"Unknown workflow action: {action}")

        except Exception:
            logger.exception("Unexpected error in handle_workflow_command")
            if update.effective_message:
                try:
                    await update.effective_message.reply_text("Workflow command error. Check logs for details.")
                except Exception as notify_exc:
                    logger.error("Failed to notify workflow command error: %s", notify_exc)

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
            "- /workflow create <request> drafts recurring workflows from natural language\n"
            "- /health shows runtime state\n"
            "- /stats shows request counters\n"
            "- Bot uses opencode serve API and keeps one session per chat\n"
            "Config via env vars: TELEGRAM_BOT_TOKEN, OPENCODE_API_BASE_URL, OPENCODE_API_PASSWORD"
        )

    async def handle_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        if not self._is_chat_allowed(chat_id):
            await update.effective_message.reply_text("This chat is not allowed to view health.")
            return
        await update.effective_message.reply_text(self.get_health_message(), parse_mode="MarkdownV2")

    async def handle_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        if not self._is_chat_allowed(chat_id):
            await update.effective_message.reply_text("This chat is not allowed to view stats.")
            return
        await update.effective_message.reply_text(self.get_stats_message(), parse_mode="MarkdownV2")

    def _is_chat_allowed(self, chat_id: int) -> bool:
        if self.config.allow_all_chats:
            return True
        if not self.config.allowed_chat_ids:
            return False
        return chat_id in self.config.allowed_chat_ids

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            if not update.effective_message or not update.effective_chat:
                logger.warning("handle_text called with missing message or chat")
                return

            chat_id = update.effective_chat.id
            if not self._is_chat_allowed(chat_id):
                try:
                    await update.effective_message.reply_text("This chat is not allowed to use this bot.")
                except Exception as reply_exc:
                    logger.error("Failed to send access denial message to chat %s: %s", chat_id, reply_exc)
                return

            prompt = (update.effective_message.text or "").strip()
            if not prompt:
                try:
                    await update.effective_message.reply_text("Please send a non-empty prompt.")
                except Exception as reply_exc:
                    logger.error("Failed to send empty prompt warning to chat %s: %s", chat_id, reply_exc)
                return

            logger.info(
                "Received prompt chat=%s update_id=%s message_id=%s len=%d",
                chat_id,
                getattr(update, "update_id", None),
                getattr(update.effective_message, "message_id", None),
                len(prompt),
            )

            if chat_id in self._pending_workflow_drafts:
                try:
                    reply = await self._handle_pending_workflow_reply(chat_id, prompt, context.application)
                except Exception as exc:
                    logger.exception("Failed to process workflow draft reply for chat %s", chat_id)
                    try:
                        await update.effective_message.reply_text("Workflow draft update failed. Check logs for details.")
                    except Exception as notify_exc:
                        logger.error("Failed to notify user of workflow draft error: %s", notify_exc)
                    return
                if reply is not None:
                    try:
                        await update.effective_message.reply_text(reply)
                    except Exception as reply_exc:
                        logger.error("Failed to send workflow draft reply to chat %s: %s", chat_id, reply_exc)
                    return

            try:
                await update.effective_message.reply_text("Request received. Sending to OpenCode API...")
            except Exception as reply_exc:
                logger.error("Failed to send ACK message to chat %s: %s", chat_id, reply_exc)
            queued = await self._enqueue_chat_prompt(chat_id, prompt, context.application)
            if queued:
                logger.info("Queued prompt task for chat=%s", chat_id)
            else:
                logger.warning("Chat queue full for chat=%s (limit=%d)", chat_id, self.config.chat_queue_max_pending)
                try:
                    await update.effective_message.reply_text(
                        "This chat has too many pending requests. Please wait for the current ones to finish."
                    )
                except Exception as reply_exc:
                    logger.error("Failed to notify chat %s about queue overflow: %s", chat_id, reply_exc)
        except Exception as exc:
            logger.exception("Unexpected error in handle_text")

    async def _enqueue_chat_prompt(self, chat_id: int, prompt: str, app: Application) -> bool:
        async with self._chat_queue_lock:
            queue = self._chat_queues.get(chat_id)
            if queue is None:
                queue = asyncio.Queue(maxsize=self.config.chat_queue_max_pending)
                self._chat_queues[chat_id] = queue

            if queue.full():
                if self.config.chat_queue_overflow_mode == "drop_oldest":
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                else:
                    return False

            queue.put_nowait((prompt, app))
            worker = self._chat_workers.get(chat_id)
            if worker is None or worker.done():
                self._chat_workers[chat_id] = asyncio.create_task(self._drain_chat_queue(chat_id))
            return True

    async def _drain_chat_queue(self, chat_id: int) -> None:
        queue = self._chat_queues.get(chat_id)
        if queue is None:
            return

        try:
            while True:
                prompt, app = await queue.get()
                try:
                    await self._run_and_respond(chat_id, prompt, app)
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            raise
        finally:
            async with self._chat_queue_lock:
                worker = self._chat_workers.get(chat_id)
                if worker is not None and worker is asyncio.current_task():
                    self._chat_workers.pop(chat_id, None)
                if queue.empty():
                    self._chat_queues.pop(chat_id, None)

    async def _send_result_messages(self, chat_id: int, result: str, app: Application) -> None:
        try:
            decorated_chunks = await self.decorate_output(result)
            if decorated_chunks:
                for chunk in decorated_chunks:
                    try:
                        await app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="MarkdownV2")
                    except Exception as send_exc:
                        logger.error("Failed to send decorated message to chat %s: %s", chat_id, send_exc)
                        try:
                            await app.bot.send_message(chat_id=chat_id, text="(decorated output could not be sent)")
                        except Exception as fallback_exc:
                            logger.error("Fallback message send also failed for chat %s: %s", chat_id, fallback_exc)
                return

            for chunk in _chunk_message(result):
                if len(chunk) > TELEGRAM_LIMIT:
                    chunk = chunk[:TELEGRAM_LIMIT]
                try:
                    escaped_chunk = _escape_markdown_v2(chunk, preserve_formatting=True)
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=escaped_chunk,
                        parse_mode="MarkdownV2",
                    )
                except Exception as send_exc:
                    logger.error("Failed to send message chunk to chat %s (len=%d): %s", chat_id, len(chunk), send_exc)
                    raise
        except Exception as exc:
            logger.exception("Error sending result messages to chat %s", chat_id)
            try:
                await app.bot.send_message(chat_id=chat_id, text="Failed to deliver OpenCode response. Check logs for details.")
            except Exception as notify_exc:
                logger.error("Could not notify user of delivery failure: %s", notify_exc)



    async def _run_and_respond(self, chat_id: int, prompt: str, app: Application) -> None:
        started_at = time.perf_counter()
        try:
            logger.info("Starting prompt execution for chat=%s", chat_id)
            async with self._semaphore:
                await app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                improved_prompt = await self.enhance_prompt(prompt)
                result = await self.run_prompt(chat_id, improved_prompt)
            await self._send_result_messages(chat_id, result, app)
            elapsed = time.perf_counter() - started_at
            logger.info("Completed prompt execution for chat=%s in %.2fs", chat_id, elapsed)

        except Exception as exc:  # broad guard to avoid silent task failures
            logger.exception("Failed to run OpenCode prompt")
            try:
                await app.bot.send_message(chat_id=chat_id, text="Unexpected error while processing your request. Check logs for details.")
            except Exception as notify_exc:
                logger.error("Could not send failure notification to chat %s: %s", chat_id, notify_exc)


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


def build_application(config: BridgeConfig, *, bridge: Optional[OpenCodeBridge] = None, workflow_manager: Any = None) -> Application:
    bridge = bridge or OpenCodeBridge(config)
    if workflow_manager is not None and hasattr(workflow_manager, "stats_lines"):
        bridge.set_workflow_stats_provider(workflow_manager.stats_lines)
        bridge.set_workflow_manager(workflow_manager)

    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("help", "Show usage help"),
        BotCommand("health", "Show runtime health"),
        BotCommand("stats", "Show request stats"),
        BotCommand("workflow", "Manage workflows"),
    ]

    async def _post_init(application: Application) -> None:
        try:
            await application.bot.set_my_commands(commands)
            logger.info("Published %d Telegram commands", len(commands))
        except Exception:
            logger.exception("Failed to publish Telegram command menu")

        if workflow_manager is not None:
            try:
                logger.info("Starting workflow manager...")
                await workflow_manager.start(application.bot)
                logger.info("Workflow manager started successfully")
            except Exception as exc:
                logger.exception("Failed to start workflow manager during application initialization")
                raise RuntimeError(f"Workflow manager startup failed: {exc}") from exc

    async def _post_shutdown(application: Application) -> None:
        if workflow_manager is not None:
            try:
                logger.info("Stopping workflow manager...")
                await workflow_manager.stop()
                logger.info("Workflow manager stopped successfully")
            except Exception as exc:
                logger.exception("Error during workflow manager shutdown")

        try:
            await bridge.close()
        except Exception:
            logger.exception("Error during bridge shutdown")

    app = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", bridge.handle_start))
    app.add_handler(CommandHandler("help", bridge.handle_help))
    app.add_handler(CommandHandler("health", bridge.handle_health))
    app.add_handler(CommandHandler("stats", bridge.handle_stats))
    app.add_handler(CommandHandler("workflow", bridge.handle_workflow_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.handle_text))
    app.add_error_handler(_handle_application_error)
    return app


def run_bridge(
    config: BridgeConfig,
    *,
    foreground: bool = True,
    log_file: Optional[Path] = None,
    workflow_manager: Any = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    configure_logging(config.log_level, log_file=log_file, foreground=foreground)
    logger.info("Starting OpenCode Telegram bridge bot")
    bridge = OpenCodeBridge(config)
    if workflow_manager is None:
        try:
            from .workflows import create_manager

            workflow_manager = create_manager(config, bridge)
        except Exception:
            workflow_manager = None
    app = build_application(config, bridge=bridge, workflow_manager=workflow_manager)

    stop_watcher: Optional[threading.Thread] = None
    if stop_event is not None:

        def _wait_for_stop() -> None:
            stop_event.wait()
            try:
                app.stop_running()
            except Exception:
                logger.exception("Failed to stop application after shutdown signal")

        stop_watcher = threading.Thread(target=_wait_for_stop, name="openbridge-stop-watcher", daemon=True)
        stop_watcher.start()

    try:
        app.run_polling(close_loop=False, stop_signals=None)
    finally:
        if stop_event is not None:
            stop_event.set()
        if stop_watcher is not None and stop_watcher.is_alive():
            stop_watcher.join(timeout=1)


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
