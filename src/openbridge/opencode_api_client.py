"""OpenCode API client for session and message management."""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger("opencode_api_client")


class OpenCodeAPIClient:
    """Handles OpenCode API interactions (sessions, messages, polling)."""

    def __init__(
        self,
        api_base_url: str,
        api_username: str,
        api_password: Optional[str],
        api_timeout_seconds: int,
        backoff_base_ms: int,
        backoff_max_ms: int,
        backoff_factor: float,
        backoff_jitter_pct: float,
    ):
        """Initialize the OpenCode API client with configuration."""
        self.api_base_url = api_base_url
        self.api_username = api_username
        self.api_password = api_password
        self.api_timeout_seconds = api_timeout_seconds
        self.backoff_base_ms = backoff_base_ms
        self.backoff_max_ms = backoff_max_ms
        self.backoff_factor = backoff_factor
        self.backoff_jitter_pct = backoff_jitter_pct

    def create_session(self) -> str:
        """Create a new session and return the session ID."""
        payload = self.request("POST", "/session", payload={})
        session_id = self._extract_session_id(payload)
        if not session_id:
            raise RuntimeError("OpenCode API did not return a session id")
        return session_id

    def run_prompt_with_polling(self, session_id: str, prompt: str, timeout_seconds: int) -> str:
        """Send a prompt and poll for a response."""
        import random
        import time

        started_at = time.time()
        logger.debug("OpenCode request start: session=%s prompt_len=%d", session_id, len(prompt))
        before_messages = self.fetch_session_messages(session_id)
        before_snapshot = set(self._extract_text_candidates(before_messages))

        immediate = self.send_session_message(session_id, prompt)
        if immediate and immediate not in before_snapshot and immediate.strip() != prompt.strip():
            elapsed = time.time() - started_at
            logger.debug("OpenCode immediate response in %.2fs", elapsed)
            return immediate

        deadline = time.time() + timeout_seconds
        poll_count = 0
        attempt = 0

        while time.time() < deadline:
            # compute adaptive backoff with optional jitter
            sleep_ms = self.backoff_base_ms * (self.backoff_factor ** attempt)
            if sleep_ms > self.backoff_max_ms:
                sleep_ms = self.backoff_max_ms
            if self.backoff_jitter_pct and self.backoff_jitter_pct > 0.0:
                jitter_factor = random.uniform(
                    max(0.0, 1.0 - self.backoff_jitter_pct), 1.0 + self.backoff_jitter_pct
                )
                sleep_ms = sleep_ms * jitter_factor

            sleep_seconds = max(0.05, float(sleep_ms) / 1000.0)
            time.sleep(sleep_seconds)
            poll_count += 1
            attempt += 1

            current = self.fetch_session_messages(session_id)
            candidates = self._extract_text_candidates(current)
            for candidate in reversed(candidates):
                if (
                    candidate not in before_snapshot
                    and candidate.strip()
                    and candidate.strip() != prompt.strip()
                ):
                    elapsed = time.time() - started_at
                    logger.debug("OpenCode response received in %.2fs after %d polls", elapsed, poll_count)
                    return candidate

            if poll_count % 10 == 0:
                elapsed = time.time() - started_at
                logger.debug("OpenCode still waiting: %.2fs elapsed (%d polls)", elapsed, poll_count)

        elapsed = time.time() - started_at
        logger.warning("OpenCode response timeout after %.2fs (%d polls)", elapsed, poll_count)
        return (
            "OpenCode API timed out waiting for a response. "
            f"Try a smaller prompt or increase OPENCODE_TIMEOUT_SECONDS (current: {timeout_seconds})."
        )

    def send_session_message(self, session_id: str, prompt: str) -> Optional[str]:
        """Send a message to a session and optionally return immediate response."""
        encoded_session = quote(session_id, safe="")
        # OpenCode currently expects message parts with a typed text object.
        # Avoid broad fallback payloads that can mask the real upstream error.
        payload_variants = [
            {"parts": [{"type": "text", "text": prompt}]},
        ]

        first_error: Optional[Exception] = None
        for payload in payload_variants:
            try:
                response = self.request(
                    "POST",
                    f"/session/{encoded_session}/message",
                    payload=payload,
                )
                candidates = self._extract_text_candidates(response)
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

    def fetch_session_messages(self, session_id: str) -> object:
        """Fetch all messages from a session."""
        encoded_session = quote(session_id, safe="")
        return self.request("GET", f"/session/{encoded_session}/message")

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> object:
        """Make a generic OpenCode API request."""
        base_url = self.api_base_url.rstrip("/")
        url = f"{base_url}{path}"
        body = None if payload is None else json.dumps(payload).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.api_password:
            raw = f"{self.api_username}:{self.api_password}".encode("utf-8")
            headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.api_timeout_seconds) as response:
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

    @staticmethod
    def _extract_session_id(payload: object) -> Optional[str]:
        """Extract session ID from API response payload."""
        if isinstance(payload, dict):
            for key in ("id", "sessionId", "session_id"):
                value = payload.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value)

            for nested_key in ("data", "result", "session"):
                nested = payload.get(nested_key)
                value = OpenCodeAPIClient._extract_session_id(nested)
                if value:
                    return value

        if isinstance(payload, list):
            for item in payload:
                value = OpenCodeAPIClient._extract_session_id(item)
                if value:
                    return value
        return None

    @staticmethod
    def _extract_text_candidates(payload: object) -> list[str]:
        """Extract text content from API response payload."""
        candidates: list[str] = []

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
                        candidates.extend(OpenCodeAPIClient._extract_text_candidates(payload.get(text_key)))

            # Prefer assistant-like roles if available.
            role = str(payload.get("role") or payload.get("type") or "").lower()
            if role in {"assistant", "ai", "response"}:
                for key in ("content", "text", "message", "output", "response"):
                    if key in payload:
                        candidates.extend(OpenCodeAPIClient._extract_text_candidates(payload.get(key)))

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
                    candidates.extend(OpenCodeAPIClient._extract_text_candidates(payload.get(key)))
            return candidates

        if isinstance(payload, list):
            for item in payload:
                candidates.extend(OpenCodeAPIClient._extract_text_candidates(item))

        return [item for item in candidates if item.strip()]
