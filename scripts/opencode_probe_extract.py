import json
import time
import urllib.parse
import urllib.request
from typing import List

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
            sid = extract_session_id(nested)
            if sid:
                return sid
    if isinstance(payload, list):
        for item in payload:
            sid = extract_session_id(item)
            if sid:
                return sid
    return None


def extract_text_candidates(payload) -> List[str]:
    candidates = []

    if isinstance(payload, str):
        text = payload.strip()
        if text:
            candidates.append(text)
        return candidates

    if isinstance(payload, dict):
        part_type = str(payload.get("type") or "").lower()
        if part_type in {"text", "input_text", "output_text"}:
            for text_key in ("text", "content", "value"):
                if text_key in payload:
                    candidates.extend(extract_text_candidates(payload.get(text_key)))

        role = str(payload.get("role") or payload.get("type") or "").lower()
        if role in {"assistant", "ai", "response"}:
            for key in ("content", "text", "message", "output", "response"):
                if key in payload:
                    candidates.extend(extract_text_candidates(payload.get(key)))

        for key in ("parts", "messages", "items", "data", "result"):
            if key in payload:
                candidates.extend(extract_text_candidates(payload.get(key)))
        return candidates

    if isinstance(payload, list):
        for item in payload:
            candidates.extend(extract_text_candidates(item))

    return candidates


create = req("POST", "/session", payload={}, timeout=30)
session_id = extract_session_id(create)
if not session_id:
    raise RuntimeError(f"No session id found in create response: {create}")

encoded_session = urllib.parse.quote(session_id, safe="")
before = req("GET", f"/session/{encoded_session}/message", timeout=30)
before_snapshot = set(extract_text_candidates(before))

_ = req(
    "POST",
    f"/session/{encoded_session}/message",
    payload={"parts": [{"type": "text", "text": PROMPT}]},
    timeout=180,
)

current = None
for _ in range(20):
    time.sleep(1)
    current = req("GET", f"/session/{encoded_session}/message", timeout=30)

all_candidates = extract_text_candidates(current)
new_candidates = [c for c in all_candidates if c not in before_snapshot and c.strip() and c.strip() != PROMPT.strip()]

print("SESSION_ID:", session_id)
print("TOTAL_CANDIDATES:", len(all_candidates))
print("NEW_CANDIDATES:", len(new_candidates))

for i, text in enumerate(new_candidates[-8:], start=max(1, len(new_candidates)-7)):
    head = text[:120].replace("\n", "\\n")
    print(f"CANDIDATE_{i}_LEN={len(text)} HEAD={head}")

if new_candidates:
    picked = new_candidates[-1]
    print("\nPICKED_BY_REVERSED_LOGIC_LEN:", len(picked))
    print("PICKED_HEAD:", picked[:500])
    print("\nPICKED_FULL_START:")
    print(picked[:3000])
