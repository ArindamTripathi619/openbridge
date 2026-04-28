import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:4096"
PROMPT = "can you give me today's international news updates?"


def req(method, path, payload=None, timeout=120):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response_body = response.read().decode("utf-8", errors="replace")
    return json.loads(response_body) if response_body.strip() else {}


def extract_session_id(payload):
    if isinstance(payload, dict):
        for key in ("id", "sessionId", "session_id"):
            value = payload.get(key)
            if value:
                return str(value)
        for nested_key in ("data", "result", "session"):
            nested = payload.get(nested_key)
            if isinstance(nested, dict):
                for key in ("id", "sessionId", "session_id"):
                    value = nested.get(key)
                    if value:
                        return str(value)
    return None


create = req("POST", "/session", payload={}, timeout=30)
session_id = extract_session_id(create)
if not session_id:
    raise RuntimeError(f"No session id found in create response: {create}")

encoded_session = urllib.parse.quote(session_id, safe="")
send = req(
    "POST",
    f"/session/{encoded_session}/message",
    payload={"parts": [{"type": "text", "text": PROMPT}]},
    timeout=180,
)

messages = None
for _ in range(20):
    time.sleep(1)
    messages = req("GET", f"/session/{encoded_session}/message", timeout=30)

print("SESSION_ID:", session_id)
print("\nCREATE_RESPONSE_JSON:")
print(json.dumps(create, ensure_ascii=False, indent=2)[:8000])
print("\nSEND_RESPONSE_JSON:")
print(json.dumps(send, ensure_ascii=False, indent=2)[:12000])
print("\nFETCH_MESSAGES_JSON_HEAD:")
print(json.dumps(messages, ensure_ascii=False, indent=2)[:50000])
