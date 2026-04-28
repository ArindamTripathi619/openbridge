"""OpenCode LLM service for prompt enhancement and output decoration."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("llm_service")


class LLMService:
    """Handles all LLM interactions (input enhancement, output decoration)."""

    def __init__(self, resolve_runtime: Callable[[str], Optional[dict]]):
        """
        Initialize LLM service.

        Args:
            resolve_runtime: Callable that returns LLM runtime config for a given stage.
                            Should return dict with keys: model, api_key, base_url, timeout_seconds,
                            or None if the stage is disabled.
        """
        self._resolve_runtime = resolve_runtime

    async def enhance_prompt(self, raw_prompt: str) -> str:
        """Enhance a user prompt using input LLM if enabled."""
        runtime = self._resolve_runtime("input")
        if not runtime:
            return raw_prompt

        try:
            rewritten = await asyncio.to_thread(self._enhance_prompt_sync, runtime, raw_prompt)
        except Exception:
            logger.exception("Input LLM rewrite failed")
            return raw_prompt

        if not rewritten:
            return raw_prompt

        return rewritten

    def _enhance_prompt_sync(self, runtime: dict, raw_prompt: str) -> Optional[str]:
        """Synchronously enhance prompt using LLM."""
        try:
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
        except (ValueError, TypeError, KeyError) as exc:
            logger.error("Input LLM prompt construction failed: %s", exc)
            return None

    async def decorate_output(self, raw_output: str) -> Optional[list[str]]:
        """Decorate OpenCode output using output LLM if enabled."""
        if not raw_output or self._is_error_result(raw_output):
            return None

        runtime = self._resolve_runtime("output")
        if not runtime:
            return None

        try:
            payload = await asyncio.to_thread(self._decorate_output_sync, raw_output, runtime)
        except Exception:
            logger.exception("Decorator post-processor failed")
            return None

        if not payload:
            return None

        return self._render_decorated_messages(payload)

    def _decorate_output_sync(self, raw_output: str, runtime: dict) -> Optional[dict]:
        """Synchronously decorate output using LLM."""
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
        """Call LLM chat completion API."""
        try:
            request = Request(
                url=f"{str(runtime['base_url']).rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {runtime['api_key']}",
                },
                method="POST",
            )
        except (ValueError, TypeError) as exc:
            logger.error("LLM request construction failed: %s", exc)
            return None

        try:
            with urlopen(request, timeout=int(runtime["timeout_seconds"])) as response:
                response_body = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("LLM request failed: %s", exc)
            return None

        try:
            response_json = json.loads(response_body)
        except json.JSONDecodeError as exc:
            logger.warning("LLM response was not valid JSON: %s", exc)
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
        """Parse LLM decorator output as JSON."""
        try:
            candidate = text.strip()
            if candidate.startswith("```"):
                candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
                if candidate.endswith("```"):
                    candidate = candidate[:-3].strip()

            start = candidate.find("{")
            end = candidate.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = candidate[start : end + 1]

            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.debug("Decorator JSON parsing failed: %s", exc)
            return None
        except (ValueError, TypeError) as exc:
            logger.debug("Decorator output processing failed: %s", exc)
            return None

        if not isinstance(parsed, dict):
            return None

        def as_string_list(value: object) -> list[str]:
            if not isinstance(value, list):
                return []
            items: list[str] = []
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

    @staticmethod
    def _is_error_result(text: str) -> bool:
        """Check if result is an error message."""
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

    @staticmethod
    def _render_decorated_messages(payload: dict) -> list[str]:
        """Render decorated output messages."""
        from .opencode_bridge import _escape_markdown_v2, OpenCodeBridge

        messages: list[str] = []

        title = _escape_markdown_v2(str(payload.get("title") or "OpenCode Result"))
        summary = _escape_markdown_v2(str(payload.get("summary") or "").strip())
        if summary:
            messages.append(f"*{title}*\n{summary}")
        else:
            messages.append(f"*{title}*")

        def render_section(label: str, items: list[str]) -> Optional[str]:
            cleaned_items = [
                OpenCodeBridge._truncate_text(item, 420) for item in items if str(item).strip()
            ]
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
