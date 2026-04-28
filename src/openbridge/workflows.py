from __future__ import annotations

import asyncio
import html
import json
import logging
import ipaddress
import os
import re
import time
import zlib
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from .opencode_bridge import BridgeConfig

logger = logging.getLogger("openbridge.workflows")

DEFAULT_WORKFLOWS_FILE = Path.home() / ".config" / "openbridge" / "workflows.json"
DEFAULT_WORKFLOWS_STATE_FILE = Path.home() / ".config" / "openbridge" / "workflows-state.json"
DEFAULT_WORKFLOW_LOOP_INTERVAL_SECONDS = 30
DEFAULT_WORKFLOW_TIMEZONE = "local"

_BLOCKED_FETCH_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


@dataclass
class WorkflowStep:
    type: str
    params: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "WorkflowStep":
        step_type = str(mapping.get("type", "")).strip()
        if not step_type:
            raise ValueError("Workflow step is missing a type")
        params = {key: value for key, value in mapping.items() if key != "type"}
        return cls(type=step_type, params=params)


@dataclass
class WorkflowDefinition:
    id: str
    name: str
    schedule: str
    enabled: bool = True
    timezone: str = DEFAULT_WORKFLOW_TIMEZONE
    targets: List[int] = field(default_factory=list)
    steps: List[WorkflowStep] = field(default_factory=list)
    retry_policy: Dict[str, Any] = field(default_factory=dict)
    dedupe_policy: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "WorkflowDefinition":
        workflow_id = str(mapping.get("id", "")).strip()
        if not workflow_id:
            raise ValueError("Workflow is missing an id")

        name = str(mapping.get("name", workflow_id)).strip() or workflow_id
        schedule = str(mapping.get("schedule", "")).strip()
        if not schedule:
            raise ValueError(f"Workflow {workflow_id} is missing a schedule")

        steps_raw = mapping.get("steps", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValueError(f"Workflow {workflow_id} must define at least one step")

        steps = [WorkflowStep.from_mapping(step) for step in steps_raw]
        targets_raw = mapping.get("targets", [])
        targets: List[int] = []
        if isinstance(targets_raw, list):
            for target in targets_raw:
                try:
                    targets.append(int(target))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Workflow {workflow_id} has an invalid target chat id: {target}") from exc

        retry_policy = mapping.get("retry_policy", {})
        dedupe_policy = mapping.get("dedupe_policy", {})
        metadata = mapping.get("metadata", {})

        if not isinstance(retry_policy, dict):
            retry_policy = {}
        if not isinstance(dedupe_policy, dict):
            dedupe_policy = {}
        if not isinstance(metadata, dict):
            metadata = {}

        return cls(
            id=workflow_id,
            name=name,
            schedule=schedule,
            enabled=bool(mapping.get("enabled", True)),
            timezone=str(mapping.get("timezone", DEFAULT_WORKFLOW_TIMEZONE)).strip() or DEFAULT_WORKFLOW_TIMEZONE,
            targets=targets,
            steps=steps,
            retry_policy=retry_policy,
            dedupe_policy=dedupe_policy,
            metadata=metadata,
        )


@dataclass
class WorkflowState:
    last_run_at: Optional[float] = None
    next_run_at: Optional[float] = None
    last_status: str = "never"
    last_error: Optional[str] = None
    run_count: int = 0
    paused: bool = False
    last_output_preview: Optional[str] = None
    last_duration_seconds: Optional[float] = None

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "WorkflowState":
        return cls(
            last_run_at=_maybe_float(mapping.get("last_run_at")),
            next_run_at=_maybe_float(mapping.get("next_run_at")),
            last_status=str(mapping.get("last_status", "never")),
            last_error=_maybe_str(mapping.get("last_error")),
            run_count=int(mapping.get("run_count", 0) or 0),
            paused=bool(mapping.get("paused", False)),
            last_output_preview=_maybe_str(mapping.get("last_output_preview")),
            last_duration_seconds=_maybe_float(mapping.get("last_duration_seconds")),
        )

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "run_count": self.run_count,
            "paused": self.paused,
            "last_output_preview": self.last_output_preview,
            "last_duration_seconds": self.last_duration_seconds,
        }


@dataclass
class WorkflowRunResult:
    workflow_id: str
    status: str
    output: str = ""
    error: Optional[str] = None
    sent_targets: List[int] = field(default_factory=list)
    duration_seconds: float = 0.0
    skipped_reason: Optional[str] = None


class WorkflowStateStore:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self._state: Dict[str, WorkflowState] = self._load()

    def _load(self) -> Dict[str, WorkflowState]:
        if not self.state_file.exists():
            return {}

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Workflow state file is invalid JSON: %s", self.state_file)
            return {}

        raw_states = payload.get("workflows", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_states, dict):
            return {}

        return {
            workflow_id: WorkflowState.from_mapping(state or {})
            for workflow_id, state in raw_states.items()
            if isinstance(state, dict)
        }

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "workflows": {
                workflow_id: state.to_mapping() for workflow_id, state in sorted(self._state.items())
            }
        }
        self.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(self.state_file.parent, 0o700)
        os.chmod(self.state_file, 0o600)

    def get(self, workflow_id: str) -> WorkflowState:
        if workflow_id not in self._state:
            self._state[workflow_id] = WorkflowState()
        return self._state[workflow_id]

    def snapshot(self) -> Dict[str, WorkflowState]:
        return dict(self._state)


def _maybe_float(value: Any) -> Optional[float]:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_str(value: Any) -> Optional[str]:
    if value in {None, ""}:
        return None
    return str(value)


def _local_now(timezone_name: str) -> datetime:
    if timezone_name.upper() == "UTC":
        return datetime.utcnow()
    return datetime.now()


def _workflow_session_chat_id(workflow_id: str) -> int:
    crc = zlib.crc32(workflow_id.encode("utf-8"))
    return -max(crc, 1)


def _is_safe_fetch_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    try:
        infos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False

    for info in infos:
        address = info[4][0]
        try:
            ip_addr = ipaddress.ip_address(address)
        except ValueError:
            continue
        for blocked_network in _BLOCKED_FETCH_NETWORKS:
            if ip_addr in blocked_network:
                return False
    return True


def _parse_daily_schedule(schedule: str) -> tuple[int, int]:
    match = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", schedule.strip())
    if not match:
        raise ValueError(f"Unsupported daily schedule: {schedule}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"Invalid daily schedule time: {schedule}")
    return hour, minute


def _parse_interval_seconds(schedule: str) -> int:
    prefix, _, remainder = schedule.partition(":")
    if prefix not in {"every", "interval"} or not remainder.strip():
        raise ValueError(f"Unsupported interval schedule: {schedule}")
    seconds = int(remainder.strip())
    if seconds <= 0:
        raise ValueError("Interval schedules must be greater than zero seconds")
    return seconds


def _parse_cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    allowed: set[int] = set()
    for part in field.split(","):
        token = part.strip()
        if not token:
            continue
        if token == "*":
            allowed.update(range(minimum, maximum + 1))
            continue
        if token.startswith("*/"):
            step = int(token[2:])
            if step <= 0:
                raise ValueError(f"Invalid cron step in field: {field}")
            allowed.update(range(minimum, maximum + 1, step))
            continue
        if "-" in token:
            start_raw, end_raw = token.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            if start > end:
                raise ValueError(f"Invalid cron range in field: {field}")
            if start < minimum or end > maximum:
                raise ValueError(f"Cron range out of bounds in field: {field}")
            allowed.update(range(start, end + 1))
            continue
        value = int(token)
        if value < minimum or value > maximum:
            raise ValueError(f"Cron value out of bounds in field: {field}")
        allowed.add(value)

    if not allowed:
        raise ValueError(f"Cron field produced no values: {field}")
    return allowed


def _parse_cron_schedule(schedule: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    expression = schedule.strip()
    if expression.startswith("cron:"):
        expression = expression[len("cron:") :].strip()

    parts = expression.split()
    if len(parts) != 5:
        raise ValueError(f"Cron schedule must have 5 fields: {schedule}")

    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    day_of_month = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    day_of_week = _parse_cron_field(parts[4].replace("7", "0"), 0, 6)
    return minute, hour, day_of_month, month, day_of_week


def _next_cron_run_timestamp(schedule: str, timezone_name: str, now: Optional[float] = None) -> float:
    minute, hour, day_of_month, month, day_of_week = _parse_cron_schedule(schedule)
    now_dt = _local_now(timezone_name) if now is None else datetime.fromtimestamp(now)
    candidate = now_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

    dom_wildcard = len(day_of_month) == 31
    dow_wildcard = len(day_of_week) == 7

    for _ in range(0, 60 * 24 * 370):
        cron_weekday = (candidate.weekday() + 1) % 7
        dom_match = candidate.day in day_of_month
        dow_match = cron_weekday in day_of_week
        day_ok = dom_match and dow_match
        if not dom_wildcard and dow_wildcard:
            day_ok = dom_match
        elif dom_wildcard and not dow_wildcard:
            day_ok = dow_match
        elif not dom_wildcard and not dow_wildcard:
            day_ok = dom_match or dow_match

        if (
            candidate.minute in minute
            and candidate.hour in hour
            and candidate.month in month
            and day_ok
        ):
            return candidate.timestamp()
        candidate += timedelta(minutes=1)

    raise ValueError(f"Could not find a matching cron time for schedule: {schedule}")


def _next_run_timestamp(workflow: WorkflowDefinition, state: WorkflowState, now: Optional[float] = None) -> Optional[float]:
    now_ts = now if now is not None else time.time()
    if workflow.schedule.startswith("daily@"):
        hour, minute = _parse_daily_schedule(workflow.schedule)
        current = _local_now(workflow.timezone)
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        scheduled_ts = scheduled.timestamp()
        if current.timestamp() <= scheduled_ts:
            return scheduled_ts
        if state.last_run_at is not None:
            last_run = datetime.fromtimestamp(state.last_run_at)
            if last_run.date() == current.date():
                return scheduled_ts + 86400
        return scheduled_ts

    if workflow.schedule.startswith("every:") or workflow.schedule.startswith("interval:"):
        interval_seconds = _parse_interval_seconds(workflow.schedule)
        if state.last_run_at is None:
            return now_ts
        return state.last_run_at + interval_seconds

    if workflow.schedule.startswith("cron:") or len(workflow.schedule.split()) == 5:
        return _next_cron_run_timestamp(workflow.schedule, workflow.timezone, now)

    raise ValueError(f"Unsupported schedule format: {workflow.schedule}")


def _workflow_is_due(workflow: WorkflowDefinition, state: WorkflowState, now: Optional[float] = None) -> bool:
    next_run_at = _next_run_timestamp(workflow, state, now)
    if next_run_at is None:
        return False
    now_ts = now if now is not None else time.time()
    return now_ts >= next_run_at


def load_workflows(path: Path) -> List[WorkflowDefinition]:
    if not path.exists():
        return []

    raw_text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Workflow file is not valid JSON: {path}") from exc

    if isinstance(payload, dict):
        workflows_raw = payload.get("workflows", [])
    elif isinstance(payload, list):
        workflows_raw = payload
    else:
        raise ValueError("Workflow file must contain a list or an object with a workflows key")

    if not isinstance(workflows_raw, list):
        raise ValueError("workflows must be a list")

    workflows = [WorkflowDefinition.from_mapping(item) for item in workflows_raw]
    workflow_ids = [workflow.id for workflow in workflows]
    if len(set(workflow_ids)) != len(workflow_ids):
        raise ValueError("Workflow ids must be unique")
    return workflows


def sample_workflows() -> Dict[str, Any]:
    return {
        "workflows": [
            {
                "id": "daily_news_digest",
                "name": "Daily News Digest",
                "enabled": True,
                "timezone": "local",
                "schedule": "daily@06:55",
                "targets": [0],
                "steps": [
                    {
                        "type": "http_fetch",
                        "normalize": "rss_digest",
                        "max_items": 12,
                        "sources": [
                            "https://feeds.bbci.co.uk/news/rss.xml",
                            "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
                        ],
                        "timeout_seconds": 20,
                    },
                    {
                        "type": "transform_python",
                        "mode": "compact_whitespace",
                    },
                    {
                        "type": "opencode_prompt",
                        "prompt_template": (
                            "You are preparing a morning news digest for Telegram. "
                            "Use the following source material and produce a concise report with:\n"
                            "- 5 to 8 top headlines\n"
                            "- 1 short summary line per item\n"
                            "- a final 'Why it matters' section\n\n"
                            "Source material:\n\n{input}"
                        ),
                    },
                    {
                        "type": "telegram_send",
                    },
                ],
            }
        ]
    }


def save_workflows(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o600)


class WorkflowManager:
    def __init__(
        self,
        *,
        config: BridgeConfig,
        bridge: Any,
        workflows_file: Path = DEFAULT_WORKFLOWS_FILE,
        state_file: Path = DEFAULT_WORKFLOWS_STATE_FILE,
        poll_interval_seconds: int = DEFAULT_WORKFLOW_LOOP_INTERVAL_SECONDS,
    ):
        self.config = config
        self.bridge = bridge
        self.workflows_file = workflows_file
        self.state_store = WorkflowStateStore(state_file)
        self.poll_interval_seconds = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._running: set[str] = set()

    def has_workflows(self) -> bool:
        try:
            return bool(load_workflows(self.workflows_file))
        except ValueError:
            return False

    def validate(self) -> List[str]:
        errors: List[str] = []
        try:
            load_workflows(self.workflows_file)
        except Exception as exc:
            errors.append(str(exc))
        return errors

    def summary_text(self) -> str:
        workflows = load_workflows(self.workflows_file)
        if not workflows:
            return f"No workflows found at {self.workflows_file}"

        now = time.time()
        lines = [f"Workflows file: {self.workflows_file}"]
        for workflow in workflows:
            state = self.state_store.get(workflow.id)
            next_run = _next_run_timestamp(workflow, state, now)
            state.next_run_at = next_run
            status = state.last_status
            if state.paused:
                status = "paused"
            enabled = "enabled" if workflow.enabled else "disabled"
            next_run_text = _format_timestamp(next_run)
            last_run_text = _format_timestamp(state.last_run_at)
            lines.append(
                f"- {workflow.id}: {workflow.name} | {enabled} | schedule={workflow.schedule} | "
                f"next={next_run_text} | last={last_run_text} | status={status} | targets={workflow.targets or 'none'}"
            )
        self.state_store.save()
        return "\n".join(lines)

    def status_text(self, workflow_id: str) -> str:
        workflows = load_workflows(self.workflows_file)
        workflow = next((item for item in workflows if item.id == workflow_id), None)
        if workflow is None:
            raise ValueError(f"Workflow not found: {workflow_id}")

        state = self.state_store.get(workflow_id)
        next_run = _next_run_timestamp(workflow, state, time.time())
        paused = "yes" if state.paused else "no"
        return (
            f"Workflow: {workflow.id}\n"
            f"Name: {workflow.name}\n"
            f"Enabled: {workflow.enabled}\n"
            f"Paused: {paused}\n"
            f"Schedule: {workflow.schedule}\n"
            f"Last run: {_format_timestamp(state.last_run_at)}\n"
            f"Next run: {_format_timestamp(next_run)}\n"
            f"Last status: {state.last_status}\n"
            f"Run count: {state.run_count}\n"
            f"Last error: {state.last_error or 'none'}"
        )

    def set_paused(self, workflow_id: str, paused: bool) -> None:
        workflows = load_workflows(self.workflows_file)
        if not any(item.id == workflow_id for item in workflows):
            raise ValueError(f"Workflow not found: {workflow_id}")
        state = self.state_store.get(workflow_id)
        state.paused = paused
        state.last_status = "paused" if paused else "idle"
        self.state_store.save()

    def stats_lines(self) -> List[str]:
        workflows = load_workflows(self.workflows_file)
        total = len(workflows)
        enabled = 0
        paused = 0
        running = len(self._running)
        successful = 0
        failed = 0
        for workflow in workflows:
            state = self.state_store.get(workflow.id)
            if workflow.enabled:
                enabled += 1
            if state.paused:
                paused += 1
            if state.last_status == "success":
                successful += 1
            if state.last_status == "failed":
                failed += 1
        return [
            f"Workflows total: {total}",
            f"Workflows enabled: {enabled}",
            f"Workflows paused: {paused}",
            f"Workflows running: {running}",
            f"Workflow last success count: {successful}",
            f"Workflow last failure count: {failed}",
        ]

    async def start(self, telegram_bot: Any) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop(telegram_bot))

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run_loop(self, telegram_bot: Any) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_due_workflows(telegram_bot)
                except Exception:
                    logger.exception("Workflow scheduler loop failed")

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def run_due_workflows(self, telegram_bot: Any) -> None:
        workflows = load_workflows(self.workflows_file)
        now = time.time()
        for workflow in workflows:
            if not workflow.enabled:
                continue
            if workflow.id in self._running:
                continue
            state = self.state_store.get(workflow.id)
            if state.paused:
                continue
            state.next_run_at = _next_run_timestamp(workflow, state, now)
            if _workflow_is_due(workflow, state, now):
                await self.run_workflow(workflow.id, telegram_bot=telegram_bot)
        self.state_store.save()

    async def run_workflow(self, workflow_id: str, *, telegram_bot: Any = None, manual: bool = False) -> WorkflowRunResult:
        try:
            workflows = load_workflows(self.workflows_file)
        except Exception as load_exc:
            logger.error("Failed to load workflows for execution of %s: %s", workflow_id, load_exc)
            return WorkflowRunResult(
                workflow_id=workflow_id,
                status="failed",
                error=f"Failed to load workflow definitions: {load_exc}",
                duration_seconds=0,
            )
        
        workflow = next((item for item in workflows if item.id == workflow_id), None)
        if workflow is None:
            logger.error("Workflow not found: %s", workflow_id)
            return WorkflowRunResult(
                workflow_id=workflow_id,
                status="failed",
                error=f"Workflow not found: {workflow_id}",
                duration_seconds=0,
            )

        if workflow.id in self._running:
            logger.info("Workflow %s is already running, skipping", workflow.id)
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="skipped",
                skipped_reason="already-running",
            )

        state = self.state_store.get(workflow.id)
        self._running.add(workflow.id)
        started_at = time.time()
        state.last_status = "running"
        state.last_error = None
        state.run_count += 1
        
        try:
            self.state_store.save()
        except Exception as save_exc:
            logger.error("Failed to save workflow state after marking running: %s", save_exc)

        try:
            output = await self._execute_workflow(workflow, telegram_bot)
            duration_seconds = time.time() - started_at
            state.last_run_at = time.time()
            state.last_status = "success"
            state.last_error = None
            state.last_output_preview = output[:500] if output else ""
            state.last_duration_seconds = duration_seconds
            try:
                state.next_run_at = _next_run_timestamp(workflow, state, time.time())
            except Exception as calc_exc:
                logger.warning("Failed to calculate next run timestamp for %s: %s", workflow.id, calc_exc)
                state.next_run_at = None
            
            try:
                self.state_store.save()
            except Exception as save_exc:
                logger.error("Failed to save workflow state after success: %s", save_exc)
            
            logger.info("Workflow %s succeeded in %.2fs", workflow.id, duration_seconds)
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="success",
                output=output,
                duration_seconds=duration_seconds,
            )
        except Exception as exc:
            duration_seconds = time.time() - started_at
            state.last_run_at = time.time()
            state.last_status = "failed"
            state.last_error = str(exc)
            state.last_duration_seconds = duration_seconds
            try:
                state.next_run_at = _next_run_timestamp(workflow, state, time.time())
            except Exception as calc_exc:
                logger.warning("Failed to calculate next run timestamp for %s after failure: %s", workflow.id, calc_exc)
                state.next_run_at = None
            
            try:
                self.state_store.save()
            except Exception as save_exc:
                logger.error("Failed to save workflow state after failure: %s", save_exc)
            
            logger.exception("Workflow %s failed after %.2fs", workflow.id, duration_seconds)
            return WorkflowRunResult(
                workflow_id=workflow.id,
                status="failed",
                error=str(exc),
                duration_seconds=duration_seconds,
            )
        finally:
            self._running.discard(workflow.id)

    async def _execute_workflow(self, workflow: WorkflowDefinition, telegram_bot: Any) -> str:
        current_text = ""
        for step in workflow.steps:
            if step.type == "http_fetch":
                current_text = await self._run_http_fetch_step(step)
            elif step.type == "transform_python":
                current_text = self._run_transform_step(current_text, step)
            elif step.type == "opencode_prompt":
                current_text = await self._run_opencode_step(workflow, current_text, step)
            elif step.type == "telegram_send":
                await self._run_telegram_send_step(workflow, current_text, step, telegram_bot)
            else:
                raise ValueError(f"Unsupported workflow step type: {step.type}")
        return current_text

    async def _run_http_fetch_step(self, step: WorkflowStep) -> str:
        sources = step.params.get("sources", [])
        if not isinstance(sources, list) or not sources:
            raise ValueError("http_fetch step requires a non-empty sources list")

        timeout_seconds = int(step.params.get("timeout_seconds", 20))
        headers = step.params.get("headers", {})
        if not isinstance(headers, dict):
            headers = {}
        normalize_mode = str(step.params.get("normalize", "auto")).strip().lower()
        max_items = int(step.params.get("max_items", 10))

        snippets: List[str] = []
        for source in sources:
            source_url = str(source).strip()
            if not source_url:
                continue
            if not _is_safe_fetch_url(source_url):
                logger.error("Blocked unsafe fetch URL: %s", source_url)
                snippets.append(f"### Source: {source_url}\n[blocked: unsafe destination]")
                continue
            try:
                fetched, content_type = await asyncio.to_thread(_fetch_url_sync, source_url, timeout_seconds, headers)
                normalized = _normalize_http_payload(
                    source_url,
                    fetched,
                    content_type=content_type,
                    normalize_mode=normalize_mode,
                    max_items=max_items,
                )
                snippets.append(normalized)
            except Exception as exc:
                logger.error("HTTP fetch failed for source %s: %s", source_url, exc)
                snippets.append(f"### Source: {source_url}\n[fetch failed: network or parsing error]")

        return "\n\n".join(snippets).strip()

    def _run_transform_step(self, current_text: str, step: WorkflowStep) -> str:
        mode = str(step.params.get("mode", "compact_whitespace")).strip().lower()
        if mode == "dedupe_lines":
            seen = set()
            lines = []
            for line in current_text.splitlines():
                cleaned = line.strip()
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                lines.append(cleaned)
            return "\n".join(lines).strip()

        if mode == "compact_whitespace":
            text = current_text.strip()
            return re.sub(r"\n{3,}", "\n\n", text)

        if mode == "identity":
            return current_text

        raise ValueError(f"Unsupported transform mode: {mode}")

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        cleaned = str(text).strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 1)].rstrip() + "…"

    def _build_bounded_opencode_prompt(self, workflow: WorkflowDefinition, current_text: str, step: WorkflowStep) -> str:
        prompt_template = str(
            step.params.get("prompt_template")
            or step.params.get("prompt")
            or "Summarize the following content for Telegram:\n\n{input}"
        )
        prompt_template = prompt_template.replace("{workflow_id}", workflow.id).replace("{workflow_name}", workflow.name)

        max_chars = int(getattr(self.config, "workflow_prompt_max_chars", 12000))
        overflow_mode = str(getattr(self.config, "workflow_prompt_overflow_mode", "reject")).strip().lower()
        if overflow_mode not in {"reject", "truncate"}:
            overflow_mode = "reject"

        if "{input}" in prompt_template:
            prefix, suffix = prompt_template.split("{input}", 1)
            static_len = len(prefix) + len(suffix)
            if static_len > max_chars:
                message = (
                    f"Workflow {workflow.id} prompt template exceeds the configured limit of {max_chars} characters"
                )
                logger.warning(message)
                if overflow_mode == "truncate":
                    return self._truncate_text(prefix + suffix, max_chars)
                raise ValueError(message)

            prompt_budget = max_chars - static_len
            if len(current_text) <= prompt_budget:
                return prefix + current_text + suffix

            message = (
                f"Workflow {workflow.id} prompt input is too large ({len(current_text)} chars > {prompt_budget} char limit)"
            )
            logger.warning(message)
            if overflow_mode == "truncate":
                return prefix + self._truncate_text(current_text, prompt_budget) + suffix
            raise ValueError(message)

        if len(prompt_template) <= max_chars:
            return prompt_template

        message = f"Workflow {workflow.id} prompt is too large ({len(prompt_template)} chars > {max_chars} char limit)"
        logger.warning(message)
        if overflow_mode == "truncate":
            return self._truncate_text(prompt_template, max_chars)
        raise ValueError(message)

    async def _run_opencode_step(self, workflow: WorkflowDefinition, current_text: str, step: WorkflowStep) -> str:
        prompt = self._build_bounded_opencode_prompt(workflow, current_text, step)
        workflow_session_id = _workflow_session_chat_id(workflow.id)
        return await self.bridge.run_prompt(workflow_session_id, prompt)

    async def _run_telegram_send_step(self, workflow: WorkflowDefinition, current_text: str, step: WorkflowStep, telegram_bot: Any) -> None:
        if telegram_bot is None:
            logger.info("Skipping telegram_send for workflow %s because no Telegram bot was provided", workflow.id)
            return

        targets_raw = step.params.get("targets", workflow.targets)
        if not isinstance(targets_raw, list):
            targets_raw = workflow.targets

        targets = []
        for target in targets_raw:
            try:
                targets.append(int(target))
            except (TypeError, ValueError):
                continue

        if not targets:
            raise ValueError(f"Workflow {workflow.id} has no Telegram targets for telegram_send")

        message = current_text.strip() or "No content produced by workflow."
        for target in targets:
            for chunk in _chunk_text(message):
                await telegram_bot.send_message(chat_id=target, text=chunk)


def _chunk_text(text: str, limit: int = 3900) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return

    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        yield text[start:end]
        start = end


def _fetch_url_sync(url: str, timeout_seconds: int, headers: Mapping[str, Any]) -> tuple[str, str]:
    request = Request(url, headers={str(key): str(value) for key, value in headers.items()})
    with urlopen(request, timeout=timeout_seconds) as response:
        raw_bytes = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        content_type = response.headers.get("Content-Type", "")
        return raw_bytes.decode(charset, errors="replace"), content_type


def _strip_html_tags(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def _extract_rss_items(payload: str, max_items: int) -> list[dict[str, str]]:
    if len(payload) > 2_000_000:
        raise ValueError("RSS payload is too large")
    if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
        raise ValueError("RSS payload contains disallowed XML declarations")

    root = ET.fromstring(payload)
    items: list[dict[str, str]] = []
    if root.tag.lower().endswith("rss"):
        for item in root.findall("./channel/item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            published = (item.findtext("pubDate") or item.findtext("published") or "").strip()
            summary = (item.findtext("description") or "").strip()
            items.append(
                {
                    "title": _strip_html_tags(title),
                    "link": link,
                    "published": _strip_html_tags(published),
                    "summary": _strip_html_tags(summary),
                }
            )
    else:
        atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("./atom:entry", atom_ns)[:max_items]:
            title = (entry.findtext("atom:title", namespaces=atom_ns) or "").strip()
            link_elem = entry.find("atom:link", atom_ns)
            link = (link_elem.get("href") if link_elem is not None else "") or ""
            published = (
                entry.findtext("atom:updated", namespaces=atom_ns)
                or entry.findtext("atom:published", namespaces=atom_ns)
                or ""
            ).strip()
            summary = (
                entry.findtext("atom:summary", namespaces=atom_ns)
                or entry.findtext("atom:content", namespaces=atom_ns)
                or ""
            ).strip()
            items.append(
                {
                    "title": _strip_html_tags(title),
                    "link": link,
                    "published": _strip_html_tags(published),
                    "summary": _strip_html_tags(summary),
                }
            )
    return items


def _normalize_http_payload(
    source_url: str,
    payload: str,
    *,
    content_type: str,
    normalize_mode: str,
    max_items: int,
) -> str:
    content_type_lower = content_type.lower()
    body = payload.strip()
    mode = normalize_mode
    if mode == "auto":
        if "rss" in content_type_lower or "atom" in content_type_lower or "<rss" in body.lower() or "<feed" in body.lower():
            mode = "rss_digest"
        elif "json" in content_type_lower:
            mode = "json"
        elif "html" in content_type_lower:
            mode = "plain_text"
        else:
            mode = "raw"

    if mode == "rss_digest":
        try:
            items = _extract_rss_items(body, max_items=max_items)
        except Exception:
            items = []
        if items:
            lines = [f"### Source: {source_url}", f"Feed items: {len(items)}"]
            for index, item in enumerate(items, start=1):
                title = item.get("title") or "(no title)"
                link = item.get("link") or ""
                published = item.get("published") or ""
                summary = item.get("summary") or ""
                lines.append(f"{index}. {title}")
                if link:
                    lines.append(f"   link: {link}")
                if published:
                    lines.append(f"   published: {published}")
                if summary:
                    lines.append(f"   summary: {summary[:280]}")
            return "\n".join(lines).strip()

    if mode == "json":
        try:
            parsed = json.loads(body)
            formatted = json.dumps(parsed, indent=2, sort_keys=True)
            return f"### Source: {source_url}\n{formatted[:12000]}"
        except Exception:
            pass

    if mode == "plain_text":
        stripped = _strip_html_tags(body)
        return f"### Source: {source_url}\n{stripped[:12000]}"

    return f"### Source: {source_url}\n{body[:12000]}"


def _format_timestamp(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def create_manager(config: BridgeConfig, bridge: Any, *, workflows_file: Path = DEFAULT_WORKFLOWS_FILE, state_file: Path = DEFAULT_WORKFLOWS_STATE_FILE) -> WorkflowManager:
    return WorkflowManager(config=config, bridge=bridge, workflows_file=workflows_file, state_file=state_file)
