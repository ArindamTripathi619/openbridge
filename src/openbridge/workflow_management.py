from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger("openbridge.workflow_management")


def workflow_file_path() -> Path:
    from .workflows import DEFAULT_WORKFLOWS_FILE

    return DEFAULT_WORKFLOWS_FILE


def slugify_workflow_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "workflow"


def extract_json_object_text(text: str) -> Optional[str]:
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


def coerce_single_workflow(payload: object) -> dict:
    if isinstance(payload, dict):
        if isinstance(payload.get("workflows"), list) and payload["workflows"]:
            first = payload["workflows"][0]
            if isinstance(first, dict):
                return dict(first)
        return dict(payload)
    raise ValueError("Workflow draft must be a JSON object")


def validate_workflow_safety(workflow_obj: dict, chat_id: int) -> list[str]:
    errors: list[str] = []

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


def format_workflow_preview(workflow_def: dict) -> str:
    from .workflows import WorkflowDefinition, WorkflowState, _next_run_timestamp

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


def _format_timestamp(timestamp: Optional[float]) -> str:
    if timestamp is None:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


async def save_workflow_definition(bridge: Any, workflow_def: dict) -> tuple[Path, bool]:
    from .workflows import save_workflows

    if hasattr(bridge, "_workflow_file_path"):
        workflows_file = bridge._workflow_file_path()
    else:
        workflows_file = workflow_file_path()
    async with bridge._workflow_file_lock:
        existing_items: list[dict] = []
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
        merged: list[dict] = []
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


async def run_workflow_now(bridge: Any, workflow_id: str, app: Any) -> str:
    if bridge._workflow_manager is None:
        return "Workflow saved, but no active workflow manager was attached."

    result = await bridge._workflow_manager.run_workflow(workflow_id, telegram_bot=app.bot, manual=True)
    if result.status == "success":
        return f"Workflow {workflow_id} executed successfully in {result.duration_seconds:.2f}s."
    if result.status == "skipped":
        return f"Workflow {workflow_id} skipped: {result.skipped_reason}"
    logger.error("Workflow %s failed: %s", workflow_id, result.error)
    return f"Workflow {workflow_id} failed. Check logs for details."


async def draft_workflow_from_instruction(
    bridge: Any,
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
    draft_text = await bridge.run_prompt(authoring_chat_id, authoring_prompt)
    if bridge._is_error_result(draft_text):
        raise ValueError(draft_text)

    json_text = extract_json_object_text(draft_text)
    if not json_text:
        raise ValueError("Could not extract workflow JSON from model output")

    parsed = json.loads(json_text)
    workflow_obj = coerce_single_workflow(parsed)

    safety_errors = validate_workflow_safety(workflow_obj, chat_id)
    if safety_errors:
        raise ValueError("Workflow safety validation failed: " + "; ".join(safety_errors))

    if not workflow_obj.get("name"):
        workflow_obj["name"] = "Telegram Workflow"
    if not workflow_obj.get("id"):
        workflow_obj["id"] = slugify_workflow_id(str(workflow_obj.get("name", "workflow")))
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


async def handle_pending_workflow_reply(bridge: Any, chat_id: int, prompt: str, app: Any) -> Optional[str]:
    pending = bridge._pending_workflow_drafts.get(chat_id)
    if not pending:
        return None

    raw = prompt.strip()
    decision = raw.upper()
    if decision == "CANCEL":
        bridge._pending_workflow_drafts.pop(chat_id, None)
        return "Workflow draft discarded."

    if decision == "YES" or decision == "RUN":
        workflow_def = pending["workflow"]
        workflows_file, replaced = await save_workflow_definition(bridge, workflow_def)
        bridge._pending_workflow_drafts.pop(chat_id, None)
        action_text = "updated" if replaced else "saved"
        message = f"Workflow {workflow_def['id']} {action_text} in {workflows_file}."
        if decision == "RUN":
            run_message = await run_workflow_now(bridge, str(workflow_def["id"]), app)
            return f"{message}\n{run_message}"
        return message

    if decision.startswith("EDIT"):
        delta = raw[4:].strip()
        if not delta:
            return "Use EDIT <changes> to revise the draft, for example: EDIT run at 07:30 and use 8 items."

        revised = await draft_workflow_from_instruction(
            bridge,
            chat_id=chat_id,
            instruction=delta,
            existing_draft=pending["workflow"],
        )
        pending["workflow"] = revised
        return format_workflow_preview(revised)

    return "You have a pending workflow draft. Reply with YES, RUN, EDIT <changes>, or CANCEL."


async def handle_workflow_command(bridge: Any, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not update.effective_message or not update.effective_chat:
            logger.warning("handle_workflow_command called with missing message or chat")
            return

        chat_id = update.effective_chat.id
        if not bridge._is_chat_allowed(chat_id):
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
                draft = await bridge._draft_workflow_from_instruction(chat_id=chat_id, instruction=instruction)
            except Exception:
                logger.exception("Workflow draft generation failed for chat %s", chat_id)
                try:
                    await update.effective_message.reply_text("Could not draft workflow. Check logs for details.")
                except Exception as reply_exc:
                    logger.error("Failed to send workflow draft error to chat %s: %s", chat_id, reply_exc)
                return

            try:
                bridge._pending_workflow_drafts[chat_id] = {"workflow": draft, "source": instruction}
                await update.effective_message.reply_text(bridge._format_workflow_preview(draft))
            except Exception:
                logger.exception("Error handling workflow draft for chat %s", chat_id)
                try:
                    await update.effective_message.reply_text("Failed to process workflow draft. Check logs.")
                except Exception as reply_exc:
                    logger.error("Failed to notify workflow draft error to chat %s: %s", chat_id, reply_exc)
            return

        if action == "list":
            if bridge._workflow_manager is not None:
                await update.effective_message.reply_text(bridge._workflow_manager.summary_text())
                return

            from .workflows import load_workflows

            workflows = load_workflows(bridge._workflow_file_path())
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
            if bridge._workflow_manager is None:
                await update.effective_message.reply_text("Workflow manager is not attached.")
                return
            try:
                text = bridge._workflow_manager.status_text(workflow_id)
            except Exception:
                logger.exception("Failed to fetch workflow status for %s", workflow_id)
                await update.effective_message.reply_text("Could not fetch workflow status. Check logs for details.")
                return
            await update.effective_message.reply_text(text)
            return

        if action == "pause":
            if bridge._workflow_manager is None:
                await update.effective_message.reply_text("Workflow manager is not attached.")
                return
            try:
                bridge._workflow_manager.set_paused(workflow_id, True)
            except Exception:
                logger.exception("Failed to pause workflow %s", workflow_id)
                await update.effective_message.reply_text("Could not pause workflow. Check logs for details.")
                return
            await update.effective_message.reply_text(f"Paused workflow: {workflow_id}")
            return

        if action == "resume":
            if bridge._workflow_manager is None:
                await update.effective_message.reply_text("Workflow manager is not attached.")
                return
            try:
                bridge._workflow_manager.set_paused(workflow_id, False)
            except Exception:
                logger.exception("Failed to resume workflow %s", workflow_id)
                await update.effective_message.reply_text("Could not resume workflow. Check logs for details.")
                return
            await update.effective_message.reply_text(f"Resumed workflow: {workflow_id}")
            return

        if action == "run":
            message = await bridge._run_workflow_now(workflow_id, context.application)
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