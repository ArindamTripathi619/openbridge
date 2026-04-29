from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, List, Mapping, Optional

from telegram.ext import Application

logger = logging.getLogger("openbridge.presentation")

TELEGRAM_LIMIT = 4096
SAFE_CHUNK = TELEGRAM_LIMIT
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
SENSITIVE_LOG_PATTERNS = (
    re.compile(r"(https?://api\.telegram\.org/bot)(\d{6,12}:[A-Za-z0-9_-]+)(/)", re.IGNORECASE),
    re.compile(r"\b(\d{6,12}:[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:sk|gsk|rk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}:[A-Za-z0-9._~+/=-]{16,}\b"),
    re.compile(
        r"(?i)\b(authorization|api[-_ ]?key|token|password|secret)\b\s*[:=]\s*([\"']?)[^\s\"']+\2"
    ),
)


@dataclass
class BridgePresentationContext:
    stats: Mapping[str, Any]
    started_at: float
    chat_sessions_count: int
    pending_workflow_drafts_count: int
    allowed_chat_ids_count: int
    opencode_api_base_url: str
    opencode_model: Optional[str]
    workflow_stats_provider: Optional[Callable[[], List[str]]]
    is_decorated_output_enabled: Callable[[], bool]
    is_input_llm_enabled: Callable[[], bool]


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


def _escape_markdown_v2(text: str, *, preserve_formatting: bool = False) -> str:
    text = str(text)

    def _escape_plain_segment(segment: str) -> str:
        if not preserve_formatting:
            return _escape_chars(segment, MDV2_SPECIAL_CHARS)

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
                        escaped_label = _escape_chars(label, MDV2_SPECIAL_CHARS)
                        escaped_url = _escape_chars(url, MDV2_SPECIAL_CHARS)
                        entity = "[" + escaped_label + "](" + escaped_url + ")"
            elif entity.startswith("*") and entity.endswith("*"):
                entity = "*" + _escape_chars(entity[1:-1], MDV2_SPECIAL_CHARS) + "*"
            elif entity.startswith("_") and entity.endswith("_"):
                entity = "_" + _escape_chars(entity[1:-1], MDV2_SPECIAL_CHARS) + "_"
            placeholders[token] = entity
            protected = protected.replace(match.group(0), token, 1)

        output_segment = _escape_chars(protected, MDV2_SPECIAL_CHARS)
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
    if text[:split].count("```") % 2 != 0:
        return -1
    return split


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


def _truncate_text(text: str, limit: int) -> str:
    cleaned = str(text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def render_decorated_messages(payload: dict) -> List[str]:
    messages: List[str] = []

    title = _escape_markdown_v2(str(payload.get("title") or "OpenCode Result"))
    summary = _escape_markdown_v2(str(payload.get("summary") or "").strip())
    if summary:
        messages.append(f"*{title}*\n{summary}")
    else:
        messages.append(f"*{title}*")

    def render_section(label: str, items: List[str]) -> Optional[str]:
        cleaned_items = [_truncate_text(item, 420) for item in items if str(item).strip()]
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


def format_health_message(context: BridgePresentationContext) -> str:
    uptime_seconds = int(time.monotonic() - context.started_at)
    uptime_hours, remainder = divmod(uptime_seconds, 3600)
    uptime_minutes, uptime_seconds = divmod(remainder, 60)
    uptime = f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

    allowed = "any chat" if context.allowed_chat_ids_count == 0 else f"{context.allowed_chat_ids_count} allowed chats"
    decorator_state = "enabled" if context.is_decorated_output_enabled() else "disabled"
    input_llm_state = "enabled" if context.is_input_llm_enabled() else "disabled"
    model = context.stats.get("last_model") or context.opencode_model or "default"
    last_error = context.stats.get("last_error") or "none"

    lines = [
        "*Health*",
        "Status: running",
        f"Uptime: {_escape_markdown_v2(uptime)}",
        f"OpenCode model: {_escape_markdown_v2(str(model))}",
        f"OpenCode API: {_escape_markdown_v2(context.opencode_api_base_url)}",
        f"Active sessions: {context.chat_sessions_count}",
        f"Input LLM rewrite: {_escape_markdown_v2(input_llm_state)}",
        f"Output decoration: {_escape_markdown_v2(decorator_state)}",
        f"Chat access: {_escape_markdown_v2(allowed)}",
        f"Last result: {_escape_markdown_v2(str(context.stats.get('last_result_kind') or 'none'))}",
        f"Last error: {_escape_markdown_v2(str(last_error))}",
    ]
    return "\n".join(lines)


def format_stats_message(context: BridgePresentationContext) -> str:
    uptime_seconds = int(time.monotonic() - context.started_at)
    uptime_hours, remainder = divmod(uptime_seconds, 3600)
    uptime_minutes, uptime_seconds = divmod(remainder, 60)
    uptime = f"{uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

    lines = [
        "*Stats*",
        f"Requests: {context.stats['requests']}",
        f"Successful: {context.stats['successful_requests']}",
        f"Failed: {context.stats['failed_requests']}",
        f"Prompt rewrites: {context.stats['prompt_rewrites']}",
        f"Input LLM failures: {context.stats['input_llm_failures']}",
        f"Decorated outputs: {context.stats['decorated_outputs']}",
        f"Decorator failures: {context.stats['decorator_failures']}",
        f"Last model: {_escape_markdown_v2(str(context.stats.get('last_model') or 'none'))}",
        f"Uptime: {_escape_markdown_v2(uptime)}",
        f"Pending workflow drafts: {context.pending_workflow_drafts_count}",
    ]
    if context.workflow_stats_provider is not None:
        try:
            workflow_lines = context.workflow_stats_provider()
        except (RuntimeError, ValueError, TypeError) as exc:
            logger.warning("Workflow stats provider failed: %s", exc)
            workflow_lines = [f"Workflows stats error: {exc}"]
        except Exception as exc:
            logger.error("Unexpected error in workflow stats provider: %s", exc)
            workflow_lines = ["Workflows stats unavailable"]
        if workflow_lines:
            lines.append("")
            lines.append("*Workflows*")
            lines.extend(_escape_markdown_v2(str(item)) for item in workflow_lines)
    return "\n".join(lines)


async def send_result_messages(
    chat_id: int,
    result: str,
    app: Application,
    decorate_output: Callable[[str], Awaitable[Optional[List[str]]]],
) -> None:
    try:
        decorated_chunks = await decorate_output(result)
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
    except Exception:
        logger.exception("Error sending result messages to chat %s", chat_id)
        try:
            await app.bot.send_message(chat_id=chat_id, text="Failed to deliver OpenCode response. Check logs for details.")
        except Exception as notify_exc:
            logger.error("Could not notify user of delivery failure: %s", notify_exc)