#!/usr/bin/env python3
"""Smoke-test the Evan loopback candidate. Prints statuses and counts only.

usage (root on Oracle): smoke_evan_candidate.py [base_url] [env_file]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18800"
ENV_FILE = sys.argv[2] if len(sys.argv) > 2 else "/etc/evan-memory-candidate.env"

env = {}
for line in open(ENV_FILE, encoding="utf-8"):
    if "=" in line and not line.startswith("#"):
        key, value = line.rstrip("\n").split("=", 1)
        env[key] = value
TOKEN = env.get("OMBRE_AUTH_TOKEN", "")

results = []


def call(method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Accept", "application/json, text/event-stream")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


status, body, _ = call("GET", "/health")
health = json.loads(body) if status == 200 else {}
check("health 200", status == 200, f"buckets={health.get('buckets')} read_only={health.get('memory_read_only')}")

for path in ("/", "/letters", "/play/", "/dashboard", "/chat"):
    status, _, headers = call("GET", path)
    check(f"GET {path} reachable", status in (200, 302, 303, 307, 401), f"status={status}")

status, _, _ = call("GET", "/gale-dash/dashboard")
check("gale-dash blocked", status == 404, f"status={status}")
status, _, _ = call("POST", "/api/night_fall/generate_gale", {})
check("generate_gale blocked", status == 404, f"status={status}")

status, body, _ = call(
    "POST",
    "/api/recall",
    {"query": "鹦鹉", "max_results": 3, "include_recent": 2, "max_tokens": 1500},
)
text = json.loads(body).get("text", "") if status == 200 else ""
check("REST recall", status == 200 and text.count("[bucket_id:") >= 1, f"status={status} buckets_in_text={text.count('[bucket_id:')} chars={len(text)}")

init = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "0"}},
}
status, _, _ = call("POST", "/mcp", init)
check("MCP without token rejected", status == 401, f"status={status}")
status, body, headers = call("POST", "/mcp", init, {"Authorization": f"Bearer {TOKEN}"})
session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id", "")
check("MCP initialize with token", status == 200 and bool(session), f"status={status}")
if session:
    auth = {"Authorization": f"Bearer {TOKEN}", "Mcp-Session-Id": session}
    call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, auth)
    status, body, _ = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, auth)
    raw = body.decode("utf-8", "replace")
    payload = raw[raw.find("{"):] if "data:" in raw else raw
    try:
        tools = [tool["name"] for tool in json.loads(payload.splitlines()[0])["result"]["tools"]]
    except Exception:
        tools = []
    check("MCP tools/list", status == 200 and "breath" in tools and "hold" in tools, f"tools={len(tools)}")
    leaked = [name for name in tools if name.startswith("state_")]
    check("no Gale state tools", not leaked, f"{leaked}")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
