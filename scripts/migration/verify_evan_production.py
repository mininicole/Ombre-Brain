#!/usr/bin/env python3
"""Verify Evan production on loopback with real secrets (run as root on Oracle).

Prints statuses, counts and ids only. Test conversations are deleted.
usage: verify_evan_production.py [--chat]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sys
import urllib.error
import urllib.request

OMBRE = "http://127.0.0.1:8800"
CHAT = "http://127.0.0.1:8787"
env = {}
for line in open("/etc/evan-memory.env", encoding="utf-8"):
    if "=" in line:
        k, v = line.rstrip("\n").split("=", 1)
        env[k] = v[1:-1] if v.startswith('"') and v.endswith('"') else v

ok_all = True


def check(name, ok, detail=""):
    global ok_all
    ok_all &= bool(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def call(method, url, body=None, headers=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


status, body, _ = call("GET", OMBRE + "/health")
check("ombre health", status == 200, body.decode()[:120])

status, body, _ = call("GET", OMBRE + "/api/pinned?max_tokens=3000")
pinned = json.loads(body) if status == 200 else {}
check("/api/pinned", status == 200 and pinned.get("count", 0) >= 1, f"count={pinned.get('count')}")

query = "？？？？？？你给我赚回来？今天那个大项目才胎死腹中"
base = {"query": query, "max_tokens": 1500, "max_results": 3, "include_recent": 2, "surface_dreams": False, "domain": ""}
for include in (True, False):
    status, body, _ = call("POST", OMBRE + "/api/recall", dict(base, include_pinned=include))
    text = json.loads(body).get("text", "") if status == 200 else ""
    ids = re.findall(r"bucket_id:([0-9a-z]+)", text)
    project = [i for i in ids if i in ("ed7f4ba9d1b4", "0be3d6e3299a")]
    label = "recall include_pinned=%s" % include
    if include:
        check(label, status == 200, f"ids={len(ids)} project_hits={project}")
    else:
        check(label + " finds the project", status == 200 and project, f"ids={len(ids)} project_hits={project}")

tok = env.get("OMBRE_AUTH_TOKEN", "")
init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0"}}}
status, _, _ = call("POST", OMBRE + "/mcp", init)
check("MCP without token 401", status == 401, f"status={status}")
status, _, _ = call("POST", OMBRE + "/mcp", init, {"Authorization": f"Bearer {tok}"})
check("MCP with production token", status == 200, f"status={status}")

if "--chat" in sys.argv:
    chat_token = hmac.new(env["CHAT_SECRET"].encode(), b"chat-v1", hashlib.sha256).hexdigest()
    auth = {"Authorization": f"Bearer {chat_token}"}
    status, body, _ = call("GET", CHAT + "/api/models", headers=auth)
    models = [m["id"] for m in json.loads(body).get("models", [])] if status == 200 else []
    check("chat models list", models == ["claude-opus-5-5", "claude-opus-4-6", "claude-sonnet-4-6"], f"{models}")
    for model in models:
        status, body, _ = call("POST", CHAT + "/api/chat",
                               {"message": "迁移验收测试：只回复两个字：收到", "model": model, "effort": "low"},
                               auth, timeout=240)
        raw = body.decode("utf-8", "replace")
        events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:") and l[5:].strip().startswith("{")]
        conv = next((e.get("conversation_id") for e in events if e.get("conversation_id")), None)
        types = sorted({str(e.get("type")) for e in events})
        text = "".join(str(e.get("text") or e.get("delta") or e.get("content") or "") for e in events if e.get("type") not in ("thinking", "tool", "meta"))
        errors = [str(e.get("message") or e.get("error"))[:80] for e in events if e.get("type") == "error"]
        check(f"chat {model}", status == 200 and not errors and events, f"events={len(events)} types={types} reply_chars={len(text)} errors={errors}")
        if conv:
            call("DELETE", CHAT + f"/api/sessions/{conv}", headers=auth)

print("ALL PASS" if ok_all else "SOME FAILED")
