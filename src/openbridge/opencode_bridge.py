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
from openbridge.bridge_presentation import (
    BridgePresentationContext,
    _chunk_message,
    _escape_markdown_v2,
    _find_markdown_safe_split_index,
    _redact_sensitive_text,
    format_health_message,
    format_stats_message,
    render_decorated_messages,
    send_result_messages,
)
from openbridge.workflow_management import (
    coerce_single_workflow,
    draft_workflow_from_instruction,
    extract_json_object_text,
    format_workflow_preview,
    handle_pending_workflow_reply,
    run_workflow_now,
    save_workflow_definition,
    slugify_workflow_id,
    validate_workflow_safety,
    workflow_file_path,
)
from openbridge.opencode_api_client import OpenCodeAPIClient

logger = logging.getLogger("opencode_bridge")

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


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _redact_sensitive_text(rendered)


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
        return render_decorated_messages(payload)

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        from openbridge.bridge_presentation import _truncate_text

        return _truncate_text(text, limit)

    def get_health_message(self) -> str:
        context = BridgePresentationContext(
            stats=self._stats,
            started_at=self._started_at,
            chat_sessions_count=len(self._chat_sessions),
            pending_workflow_drafts_count=len(self._pending_workflow_drafts),
            allowed_chat_ids_count=len(self.config.allowed_chat_ids),
            opencode_api_base_url=self.config.opencode_api_base_url,
            opencode_model=self.config.opencode_model,
            workflow_stats_provider=self._workflow_stats_provider,
            is_decorated_output_enabled=self._is_decorated_output_enabled,
            is_input_llm_enabled=lambda: bool(self._resolve_llm_runtime("input")),
        )
        return format_health_message(context)

    def get_stats_message(self) -> str:
        context = BridgePresentationContext(
            stats=self._stats,
            started_at=self._started_at,
            chat_sessions_count=len(self._chat_sessions),
            pending_workflow_drafts_count=len(self._pending_workflow_drafts),
            allowed_chat_ids_count=len(self.config.allowed_chat_ids),
            opencode_api_base_url=self.config.opencode_api_base_url,
            opencode_model=self.config.opencode_model,
            workflow_stats_provider=self._workflow_stats_provider,
            is_decorated_output_enabled=self._is_decorated_output_enabled,
            is_input_llm_enabled=lambda: bool(self._resolve_llm_runtime("input")),
        )
        return format_stats_message(context)

    @staticmethod
    def _slugify_workflow_id(value: str) -> str:
        return slugify_workflow_id(value)

    @staticmethod
    def _extract_json_object_text(text: str) -> Optional[str]:
        return extract_json_object_text(text)

    @staticmethod
    def _coerce_single_workflow(payload: object) -> dict:
        return coerce_single_workflow(payload)

    @staticmethod
    def _workflow_file_path() -> Path:
        return workflow_file_path()

    async def _draft_workflow_from_instruction(
        self,
        *,
        chat_id: int,
        instruction: str,
        existing_draft: Optional[dict] = None,
    ) -> dict:
        return await draft_workflow_from_instruction(
            self,
            chat_id=chat_id,
            instruction=instruction,
            existing_draft=existing_draft,
        )

    @staticmethod
    def _validate_workflow_safety(workflow_obj: dict, chat_id: int) -> List[str]:
        return validate_workflow_safety(workflow_obj, chat_id)

    def _format_workflow_preview(self, workflow_def: dict) -> str:
        return format_workflow_preview(workflow_def)

    async def _save_workflow_definition(self, workflow_def: dict) -> tuple[Path, bool]:
        return await save_workflow_definition(self, workflow_def)

    async def _run_workflow_now(self, workflow_id: str, app: Application) -> str:
        return await run_workflow_now(self, workflow_id, app)

    async def _handle_pending_workflow_reply(self, chat_id: int, prompt: str, app: Any) -> Optional[str]:
        return await handle_pending_workflow_reply(self, chat_id, prompt, app)

    async def handle_workflow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from .workflow_management import handle_workflow_command

        await handle_workflow_command(self, update, context)

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
        await send_result_messages(chat_id, result, app, self.decorate_output)



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
