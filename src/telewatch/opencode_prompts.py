def storm_prompt(
    log_path: str,
    log_tail: str,
    alert_count: int,
    window_seconds: int,
    keywords: list[str],
) -> str:
    kw = ', '.join(keywords) if keywords else 'not specified'
    return f"""You are analyzing a server process log on a remote VM.
Log file: {log_path}

An alert storm was detected: {alert_count} alerts fired in {window_seconds} seconds.
Triggering keywords: {kw}

Recent log output (last 200 lines):
---
{log_tail}
---

Tasks:
1. Identify the root cause of the alert storm.
2. Assess severity: is this FATAL, RECOVERABLE, or NOISE?
3. Give one concrete immediate action if needed.
4. Write a clean 3-5 line plain-text summary for Telegram.

Rules:
- No markdown headers or bold syntax.
- Plain text only.
- Be concise. Do not pad.
"""


def manual_prompt(
    user_request: str,
    log_path: str,
    process_name: str,
) -> str:
    return f"""You are running on a remote server VM.
Monitored log: {log_path}
Monitored process: {process_name}

User request: {user_request}

You have access to the filesystem and can run shell commands.
Execute whatever is needed to answer the request.
Respond with findings only — no preamble, no padding.
"""
