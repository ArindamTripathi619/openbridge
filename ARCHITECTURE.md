## Architecture & Design

This document describes the OpenBridge architecture, module responsibilities, and guidelines for maintaining clean boundaries.

### Module Responsibilities

#### OpenCodeBridge (opencode_bridge.py)
**Responsibility**: Telegram message handling, service orchestration, session management, statistics.

**Public API**:
- `async run_prompt(chat_id, prompt)` - Main entry point for forwarding to OpenCode
- `async enhance_prompt(raw_prompt)` - Optional input LLM enhancement
- `async decorate_output(raw_output)` - Optional output LLM formatting
- `/health`, `/stats` - Observability endpoints
- Telegram command handlers

**Boundaries**: 
- Does NOT directly call OpenCode API (delegates to `OpenCodeAPIClient`)
- Does NOT implement LLM logic (delegates to `LLMService`)
- Does NOT handle message rendering internals (delegates to `bridge_presentation.py`)
- Does NOT own workflow authoring/execution internals (delegates to `workflow_management.py`)
- Focuses on: dispatch, error recovery, session tracking, stats

#### bridge_presentation.py
**Responsibility**: Telegram-facing rendering, chunking, redaction, and delivery helpers.

**Public API**:
- `format_health_message(context)` - Render `/health`
- `format_stats_message(context)` - Render `/stats`
- `render_decorated_messages(payload)` - Format LLM decoration payloads
- `send_result_messages(chat_id, result, app, decorate_output)` - Deliver responses safely

**Boundaries**:
- Does NOT talk to OpenCode directly
- Does NOT manage workflow state
- Does NOT own Telegram dispatch logic

#### workflow_management.py
**Responsibility**: Workflow drafting, validation, persistence, execution, and command handling.

**Public API**:
- `draft_workflow_from_instruction(bridge, chat_id, instruction, existing_draft=None)` - Draft workflow JSON
- `save_workflow_definition(bridge, workflow_def)` - Persist workflow definitions
- `handle_workflow_command(bridge, update, context)` - Process `/workflow` commands
- `handle_pending_workflow_reply(bridge, chat_id, prompt, app)` - Handle draft replies

**Boundaries**:
- Does NOT own Telegram application setup
- Does NOT perform OpenCode transport calls directly
- Does NOT format general chat output

#### OpenCodeAPIClient (opencode_api_client.py)
**Responsibility**: All OpenCode API interactions.

**Public API**:
- `create_session()` - POST /session
- `send_session_message(session_id, prompt)` - POST /session/{id}/message
- `fetch_session_messages(session_id)` - GET /session/{id}/message
- `run_prompt_with_polling(session_id, prompt, timeout)` - Orchestrates send + poll with backoff
- `request(method, path, payload)` - Generic API call

**Boundaries**:
- Does NOT enforce OpenCode business logic (stateless)
- Does NOT track sessions across chats (caller's responsibility)
- Does NOT handle Telegram concerns
- Focuses on: HTTP semantics, error translation, retry strategies, JSON marshaling

#### LLMService (llm_service.py)
**Responsibility**: All LLM interactions (input enhancement, output decoration).

**Public API**:
- `async enhance_prompt(raw_prompt)` - Input enhancement
- `async decorate_output(raw_output)` - Output formatting
- Takes a `resolve_runtime` callback to fetch LLM config on demand

**Boundaries**:
- Does NOT depend on OpenCodeAPIClient or OpenCodeBridge
- Does NOT manage session state
- Does NOT handle Telegram message formatting
- Focuses on: LLM API calls, JSON parsing, graceful error handling, output validation

### Dependency Graph

```
Telegram API
     ↓
OpenCodeBridge (orchestrator)
     ├─→ OpenCodeAPIClient (stateless)
     ├─→ LLMService (stateless, config-injected)
   ├─→ workflow_management.py (workflow orchestration)
   ├─→ bridge_presentation.py (rendering / delivery helpers)
     └─→ Config (BridgeConfig)
```

**Key Property**: Services flow downward; no upward dependencies or circular references.

### Adding New Features

#### Adding a new LLM capability?
→ Extend `LLMService`

#### Adding a new OpenCode API endpoint?
→ Extend `OpenCodeAPIClient`

#### Adding a new Telegram command or adjustment to message dispatch?
→ Extend `OpenCodeBridge`, keep specific logic delegated to `bridge_presentation.py` or `workflow_management.py`

#### Changing polling backoff behavior?
→ Modify `OpenCodeAPIClient.run_prompt_with_polling()` and config knobs

### Drift Checks (Review Checklist Item)

Before merging changes, verify:

1. **Service Boundaries**: 
   - [ ] No direct HTTP calls in `OpenCodeBridge` (use `OpenCodeAPIClient`)
   - [ ] No LLM logic outside `LLMService` (use service or helper)
   - [ ] No session tracking in `OpenCodeAPIClient` (stateless by design)

2. **Imports**:
   - [ ] `OpenCodeAPIClient` does NOT import `OpenCodeBridge`
   - [ ] `LLMService` does NOT import `OpenCodeBridge` or `OpenCodeAPIClient`
   - [ ] `OpenCodeBridge` imports both services cleanly

3. **Responsibilities**:
   - [ ] `OpenCodeBridge` stays focused on: dispatch, orchestration, stats
   - [ ] `OpenCodeAPIClient` stays focused on: HTTP, sessions, polling
   - [ ] `LLMService` stays focused on: LLM calls, JSON parsing

4. **Error Handling**:
   - [ ] Specific exceptions in service methods (not broad `except Exception`)
   - [ ] Root cause preserved in logs (not swallowed)
   - [ ] Top-level handlers remain for unexpected failures

5. **Testing**:
   - [ ] Service methods testable in isolation
   - [ ] No tight coupling to Telegram or OpenCode in service tests

### Phase 2 Modularization (Complete)

Phase 2 has been implemented:
1. Rendering, chunking, and redaction now live in `bridge_presentation.py`
2. Workflow drafting, persistence, execution, and command handling now live in `workflow_management.py`
3. `OpenCodeBridge` now acts as a thin composition root with compatibility wrappers
4. The bridge class no longer owns the extracted presentation or workflow internals

Current outcome: OpenCodeBridge is focused on orchestration, while the extracted modules own the specialized logic.

### References

- **Service Locator Pattern**: How configuration is injected (see `_resolve_llm_runtime`)
- **Adapter Pattern**: How `OpenCodeAPIClient` translates HTTP errors to runtime exceptions
- **Lazy Initialization**: Per-chat sessions created on first use
- **Graceful Degradation**: Services fail safely (e.g., LLM failure doesn't block OpenCode call)
