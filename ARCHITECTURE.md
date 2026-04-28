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
- Does NOT handle message rendering internals (uses helper functions)
- Focuses on: dispatch, error recovery, session tracking, stats

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
     ├─→ Workflow (internal)
     ├─→ Message rendering (utilities)
     └─→ Config (BridgeConfig)
```

**Key Property**: Services flow downward; no upward dependencies or circular references.

### Adding New Features

#### Adding a new LLM capability?
→ Extend `LLMService`

#### Adding a new OpenCode API endpoint?
→ Extend `OpenCodeAPIClient`

#### Adding a new Telegram command or adjustment to message dispatch?
→ Extend `OpenCodeBridge`, keep specific logic delegated to services

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

### Phase 2 Modularization (Future)

When ready, Phase 2 will:
1. Extract rendering module (message formatting, chunking)
2. Extract workflow module (authoring, scheduling, execution)
3. Extract stats/telemetry module
4. Integrate new modules into OpenCodeBridge as clean composition root
5. Remove redundant code from main bridge class

Expected outcome: OpenCodeBridge reduces from 1700+ LOC to <600 LOC focused on orchestration.

### References

- **Service Locator Pattern**: How configuration is injected (see `_resolve_llm_runtime`)
- **Adapter Pattern**: How `OpenCodeAPIClient` translates HTTP errors to runtime exceptions
- **Lazy Initialization**: Per-chat sessions created on first use
- **Graceful Degradation**: Services fail safely (e.g., LLM failure doesn't block OpenCode call)
