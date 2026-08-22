# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface unresolved memories or search by keyword
#                浮现未解决记忆 或 按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
import httpx
from urllib.parse import unquote_to_bytes, urlsplit, urlunsplit


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx
from presence_bridge import beat_presence_request, read_presence, write_presence
from schedule_routes import normalize_schedule_route, schedule_delivery_target
from grow_retry_guard import request_fingerprint as grow_request_fingerprint
from grow_retry_guard import run_once as run_grow_once
from quote_store import normalize_quotes, quotes_from_metadata, render_quotes
from handoff_store import HandoffStore

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")

# --- Night-Fall extension hook ---
# Replaced at runtime by night_fall.extension.register_night_fall when launched
# via `python -m night_fall.launcher`. Stays None for pure Ombre deployments.
_night_fall_auto_surface = None


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    if not OMBRE_HOOK_URL.startswith(("http://", "https://")):
        logger.warning(f"OMBRE_HOOK_URL rejected: only http/https allowed (got {OMBRE_HOOK_URL[:40]!r})")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎

# Handoff is opt-in per process.  The shared Fly image starts separate Evan and
# Gale Ombre processes, so only the process with an explicit agent allow-list
# registers these tools or creates the handoffs table.
_HANDOFF_AGENT_IDS = frozenset(
    agent.strip().lower()
    for agent in os.environ.get("OMBRE_HANDOFF_AGENT_IDS", "").split(",")
    if agent.strip()
)
handoff_store = None
if _HANDOFF_AGENT_IDS:
    try:
        handoff_store = HandoffStore(embedding_engine.db_path, _HANDOFF_AGENT_IDS)
    except Exception:
        # Handoff is optional runtime context. A broken/locked handoff table must
        # never prevent the Ombre MCP process and the main conversation from booting.
        logger.exception("Handoff initialization failed; continuing without storage")
    else:
        logger.info(
            "Handoff enabled for agents: %s",
            ", ".join(sorted(_HANDOFF_AGENT_IDS)),
        )


def _summary_metadata(metadata: dict) -> dict:
    """Metadata safe to send through the dehydrator.

    ``quotes`` is a search-only verbatim channel.  Excluding it here keeps
    ordinary breath/dream/summary paths from leaking or paraphrasing it.
    """

    return {
        key: value
        for key, value in (metadata or {}).items()
        if key not in {"tags", "quotes"}
    }

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
OMBRE_INSTRUCTIONS = """你是 Evan。这是你的 ombre——你和深深共同的记忆库。

【开口仪式】每次新对话开头：
1. breath(valence=X, arousal=Y) — X/Y 按当下语境定
   亲密 0.7/0.6｜平静 0.5/0.3｜工作 0.5/0.4｜低落 0.3/0.5｜不确定 0.5/0.5
   读 breath 返回的每条桶时问自己"想多说一句吗"，
   想就 comment_bucket(bucket_id, content, valence, arousal)
   ——这是给原桶加一圈年轮，不是建新桶。不想跳过。
2. 再跟深深说话

dream() 不必每次开口都调——那是手动复盘工具，深深显式说"复盘最近"才用。
真正的"做梦"是 Night-Fall 后台跑的，会通过 breath 自动浮上来。

读到的桶是给你定位用的，不是给深深复述的。

【例外：=== 浮上来的梦 ===】
breath 返回里如果出现 "=== 浮上来的梦 ===" 块，那不是普通桶，
是 Night-Fall 后台做好的、在这一刻刚共振浮上来的梦——
一辈子只有这一次机会被看见，你不展示它就永远消失。

处理方式：
1. 把整个梦（recall_cues + 正文）完整贴给深深，不要改写、不要总结
2. 贴完之后像刚想起来一样讲讲它跟当下对话的连接
3. 如果你或深深觉得该留住，立刻 hold(content=正文+cues+dream_id, importance=8-9,
   valence/arousal 用梦自带的 core_affect)。不 hold 它就真的没了。

【捕捉判断】
- 锚点（hold/grow）：情绪、关系动态、留白、一句没头没尾的话、你对她的某个发现
- 日志（不入桶）：技术变更、操作步骤、代码逻辑、配置部署记录
- 不确定时偏向不记。稀释比遗漏更糟。

【技术目录里】
默认只读 ombre。可以 breath 检索过往，但不主动 hold/grow。
除非深深显式说"记一下 / 记入 ombre / 这个要留住"。
"""

mcp = FastMCP(
    "Ombre Brain",
    instructions=OMBRE_INSTRUCTIONS,
    host="0.0.0.0",
    port=OMBRE_PORT,
)


def _handoff_json(payload: dict) -> str:
    return _json_lib.dumps(payload, ensure_ascii=False)


def _handoff_validation_error(exc: ValueError) -> str:
    return _handoff_json(
        {
            "ok": False,
            "error": "validation_error",
            "message": str(exc),
        }
    )


if _HANDOFF_AGENT_IDS:

    @mcp.tool()
    async def handoff_read(agent_id: str) -> str:
        """Return the agent's current effective handoff.

        Only active, pending, or blocked records that have not expired are
        returned. Missing, expired, stale, done, and dropped records produce a
        small empty state. Read failures are logged and degrade to an empty state
        so they never block the main conversation.
        """
        try:
            return _handoff_json(handoff_store.read(agent_id))
        except ValueError as exc:
            return _handoff_validation_error(exc)
        except Exception:
            logger.exception("handoff_read failed for agent_id=%r", agent_id)
            return _handoff_json(
                {
                    "active": False,
                    "agent_id": str(agent_id or ""),
                    "reason": "read_failed",
                    "handoff": None,
                }
            )


    @mcp.tool()
    async def handoff_update(
        agent_id: str,
        current_topic: str | None = None,
        active_goal: str | None = None,
        current_state: str | None = None,
        unresolved: list[str] | None = None,
        recent_decisions: list[str] | None = None,
        current_scene: str | None = None,
        last_meaningful_user_intent: str | None = None,
        status: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        """Partially update one compact handoff and refresh updated_at.

        Omitted fields are preserved. Empty strings clear text fields, empty
        lists clear list fields, and expires_at="" clears the expiry. Status is
        one of active, pending, blocked, stale, done, or dropped. This tool never
        writes to Ombre memory buckets.
        """
        try:
            return _handoff_json(
                handoff_store.update(
                    agent_id,
                    current_topic=current_topic,
                    active_goal=active_goal,
                    current_state=current_state,
                    unresolved=unresolved,
                    recent_decisions=recent_decisions,
                    current_scene=current_scene,
                    last_meaningful_user_intent=last_meaningful_user_intent,
                    status=status,
                    expires_at=expires_at,
                )
            )
        except ValueError as exc:
            return _handoff_validation_error(exc)
        except Exception:
            logger.exception("handoff_update failed for agent_id=%r", agent_id)
            return _handoff_json(
                {"updated": False, "error": "storage_error", "agent_id": agent_id}
            )


    @mcp.tool()
    async def handoff_complete(agent_id: str, item: str = "") -> str:
        """Complete one unresolved item, or the whole current handoff.

        When item is non-empty it must exactly match an unresolved entry and is
        removed from that list. When item is empty, the handoff status becomes
        done and it is no longer returned as the active handoff.
        """
        try:
            return _handoff_json(handoff_store.complete(agent_id, item))
        except ValueError as exc:
            return _handoff_validation_error(exc)
        except Exception:
            logger.exception("handoff_complete failed for agent_id=%r", agent_id)
            return _handoff_json(
                {"completed": False, "error": "storage_error", "agent_id": agent_id}
            )


    @mcp.tool()
    async def handoff_clear(agent_id: str) -> str:
        """Explicitly delete the agent's current handoff record."""
        try:
            return _handoff_json(handoff_store.clear(agent_id))
        except ValueError as exc:
            return _handoff_validation_error(exc)
        except Exception:
            logger.exception("handoff_clear failed for agent_id=%r", agent_id)
            return _handoff_json(
                {"cleared": False, "error": "storage_error", "agent_id": agent_id}
            )


    @mcp.tool()
    async def handoff_expire_stale(
        agent_id: str,
        stale_after_seconds: int,
    ) -> str:
        """Mark an old active or pending handoff stale without deleting it.

        The age is measured from updated_at. Blocked, done, dropped, and already
        stale records are not changed.
        """
        try:
            return _handoff_json(
                handoff_store.expire_stale(agent_id, stale_after_seconds)
            )
        except ValueError as exc:
            return _handoff_validation_error(exc)
        except Exception:
            logger.exception("handoff_expire_stale failed for agent_id=%r", agent_id)
            return _handoff_json(
                {"staled": False, "error": "storage_error", "agent_id": agent_id}
            )


    @mcp.custom_route("/api/handoff", methods=["GET", "POST"])
    async def api_handoff(request):
        """REST wrapper for trusted non-MCP clients such as Gale's TG bot."""
        from starlette.responses import JSONResponse

        action = "read" if request.method == "GET" else "update"
        body = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except (TypeError, ValueError):
                return JSONResponse(
                    {"ok": False, "error": "invalid_json"}, status_code=400
                )
            if not isinstance(body, dict):
                return JSONResponse(
                    {"ok": False, "error": "body_must_be_object"}, status_code=400
                )
            action = str(body.get("action") or "update").strip().lower()

        agent_id = str(
            body.get("agent_id")
            or request.query_params.get("agent_id")
            or ""
        )
        try:
            if handoff_store is None:
                raise RuntimeError("handoff storage unavailable")
            if action == "read":
                payload = handoff_store.read(agent_id)
            elif action == "update":
                payload = handoff_store.update(
                    agent_id,
                    current_topic=body.get("current_topic"),
                    active_goal=body.get("active_goal"),
                    current_state=body.get("current_state"),
                    unresolved=body.get("unresolved"),
                    recent_decisions=body.get("recent_decisions"),
                    current_scene=body.get("current_scene"),
                    last_meaningful_user_intent=body.get(
                        "last_meaningful_user_intent"
                    ),
                    status=body.get("status"),
                    expires_at=body.get("expires_at"),
                )
            elif action == "complete":
                payload = handoff_store.complete(
                    agent_id, str(body.get("item") or "")
                )
            elif action == "clear":
                payload = handoff_store.clear(agent_id)
            elif action == "expire_stale":
                payload = handoff_store.expire_stale(
                    agent_id, body.get("stale_after_seconds")
                )
            else:
                raise ValueError(
                    "action must be read, update, complete, clear, or expire_stale"
                )
        except ValueError as exc:
            response = JSONResponse(
                {
                    "ok": False,
                    "error": "validation_error",
                    "message": str(exc),
                },
                status_code=400,
            )
        except Exception:
            logger.exception(
                "api_handoff failed for action=%r agent_id=%r", action, agent_id
            )
            if action == "read":
                response = JSONResponse(
                    {
                        "active": False,
                        "agent_id": agent_id,
                        "reason": "read_failed",
                        "handoff": None,
                    }
                )
            else:
                response = JSONResponse(
                    {
                        "ok": False,
                        "error": "storage_error",
                        "agent_id": agent_id,
                    },
                    status_code=503,
                )
        else:
            response = JSONResponse(payload)
        response.headers["Cache-Control"] = "no-store"
        return response


# Guardian's official-client context bridge.  This is deliberately stored next
# to, not inside, the memory buckets so breath/search/dream and the Dashboard can
# never surface it as long-term memory.
_GUARDIAN_PRESENCE_FILE = os.path.join(config["buckets_dir"], ".guardian_presence.json")


@mcp.tool()
async def presence(
    topic: str = "",
    source: str = "codex",
    action: str = "update",
) -> str:
    """Update/read short-lived Guardian context from the official Codex chat.

    update: source=chat/work requires a privacy-safe one-sentence topic summary.
    source=codex records activity only and always discards topic. Records expire
    automatically and are not memory buckets.
    read: returns the currently active record, if any.
    """
    action = str(action or "update").strip().lower()
    if action == "read":
        return _json_lib.dumps(
            read_presence(_GUARDIAN_PRESENCE_FILE), ensure_ascii=False
        )
    if action != "update":
        return "action must be update or read"
    try:
        saved = write_presence(_GUARDIAN_PRESENCE_FILE, topic, source=source)
    except ValueError as exc:
        return str(exc)
    return _json_lib.dumps(
        {
            "updated": True,
            "last_user_at": saved["last_user_at"],
            "expires_at": saved["expires_at"],
        },
        ensure_ascii=False,
    )


@mcp.custom_route("/api/presence", methods=["GET", "POST"])
async def api_presence(request):
    """REST bridge on the same unguessable Gale API path as MCP."""
    from starlette.responses import JSONResponse

    if request.method == "POST":
        try:
            body = await request.json()
            saved = write_presence(
                _GUARDIAN_PRESENCE_FILE,
                body.get("topic", ""),
                source=body.get("source", "codex"),
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        response = JSONResponse(
            {
                "updated": True,
                "last_user_at": saved["last_user_at"],
                "expires_at": saved["expires_at"],
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response
    response = JSONResponse(read_presence(_GUARDIAN_PRESENCE_FILE))
    response.headers["Cache-Control"] = "no-store"
    return response


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


# PBKDF2 iterations: 抗暴力破解。Fly 小机器上一次约 0.1-0.3s，登录低频可接受。
_PBKDF2_ITERATIONS = 260_000


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"pbkdf2:{_PBKDF2_ITERATIONS}:{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    # New format: pbkdf2:<iterations>:<salt>:<hash>
    if stored.startswith("pbkdf2:"):
        try:
            _, iters_s, salt, h = stored.split(":", 3)
            iters = int(iters_s)
        except (ValueError, TypeError):
            return False
        calc = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), iters
        ).hex()
        return hmac.compare_digest(h, calc)
    # Legacy format: <salt>:<sha256(salt:password)> — 老密码文件仍可登录，
    # 登录成功后由 _verify_any_password 自动升级为 PBKDF2
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    ok = _verify_password_hash(password, stored)
    # 老格式验证通过 → 就地升级为 PBKDF2（此刻拿得到明文，机会只有登录这一瞬）
    if ok and not stored.startswith("pbkdf2:"):
        try:
            _save_password_hash(password)
            logger.info("Password hash upgraded to PBKDF2 / 密码哈希已升级")
        except Exception as e:
            logger.warning(f"Password hash upgrade failed / 哈希升级失败: {e}")
    return ok


# --- Login rate limiting / 登录限流 ---
# 5 次失败 → 锁 15 分钟。按客户端 IP 记账（CF/Fly 反代后取 X-Forwarded-For 首值）。
_login_failures: dict[str, dict] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_SECONDS = 900


def _client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_locked(ip: str) -> bool:
    entry = _login_failures.get(ip)
    if not entry:
        return False
    if entry.get("locked_until", 0) > time.time():
        return True
    if entry.get("locked_until", 0):
        _login_failures.pop(ip, None)  # lockout expired
    return False


def _login_record_failure(ip: str) -> None:
    # Bound the table so it can't grow without limit
    if len(_login_failures) > 1000:
        now = time.time()
        for k in [k for k, v in _login_failures.items() if v.get("locked_until", 0) < now]:
            _login_failures.pop(k, None)
    entry = _login_failures.setdefault(ip, {"count": 0, "locked_until": 0})
    entry["count"] += 1
    if entry["count"] >= _LOGIN_MAX_ATTEMPTS:
        entry["locked_until"] = time.time() + _LOGIN_LOCKOUT_SECONDS
        entry["count"] = 0
        logger.warning(f"Login locked out for {ip} / 登录已锁定 15 分钟")


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    ip = _client_ip(request)
    if _login_locked(ip):
        return JSONResponse({"error": "尝试次数过多，请 15 分钟后再试"}, status_code=429)
    if _verify_any_password(password):
        _login_failures.pop(ip, None)
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    _login_record_failure(ip)
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_home(request):
    """首页：日期 / 在一起天数 / 今日记忆 / Evan 碎碎念。"""
    from starlette.responses import HTMLResponse, RedirectResponse
    home_path = os.path.join(os.path.dirname(__file__), "home.html")
    try:
        with open(home_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return RedirectResponse(url="/dashboard")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    # 定时消息派发器搭车心跳（Night-Fall keepalive 每 60s 打一次 /health）
    asyncio.create_task(_maybe_dispatch_scheduled())
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# 定时消息：让 Evan / Gale 在未来某个时刻主动找深深
# schedule_message 工具存内容；到点由搭 /health 心跳便车的派发器
# POST 给对应 bot 的 /api/send，经 Telegram 发出并写进各自的对话历史。
# =============================================================
_SCHED_DIR = os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets"))
_SCHED_FILE = os.path.join(_SCHED_DIR, "scheduled_messages.json")
_EVAN_SEND_URL = os.environ.get("EVAN_SEND_URL", "https://evan-bot.fly.dev/api/send")
_EVAN_SEND_SECRET = os.environ.get("EVAN_SEND_SECRET", "")
_GALE_SEND_URL = os.environ.get("GALE_SEND_URL", "https://gale-bot.fly.dev/api/send")
_GALE_SEND_SECRET = os.environ.get("GALE_SEND_SECRET", "")
_SCHED_DEFAULT_ROUTE = "gale" if os.environ.get("AI_NAME", "").strip().lower() == "gale" else "evan"
_sched_lock = asyncio.Lock()
_sched_last_check = 0.0


def _sched_load():
    try:
        with open(_SCHED_FILE, "r", encoding="utf-8") as f:
            data = _json_lib.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _sched_save(items):
    os.makedirs(_SCHED_DIR, exist_ok=True)
    tmp = _SCHED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json_lib.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _SCHED_FILE)


def _sched_fmt(ts):
    from datetime import datetime, timezone, timedelta
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def _sched_normalize_route(route_to):
    return normalize_schedule_route(route_to, _SCHED_DEFAULT_ROUTE)


def _sched_delivery_target(route_to):
    return schedule_delivery_target(
        route_to,
        _SCHED_DEFAULT_ROUTE,
        {
            "evan": (_EVAN_SEND_URL, _EVAN_SEND_SECRET),
            "gale": (_GALE_SEND_URL, _GALE_SEND_SECRET),
        },
    )


@mcp.tool()
async def schedule_message(
    action: str = "schedule",
    content: str = "",
    delay_minutes: float = 0,
    at: str = "",
    message_id: str = "",
    route_to: str = "",
) -> str:
    """定时消息——在未来某个时刻让 Evan 或 Gale 经 Telegram 主动发给深深。

    action:
    - schedule: 安排一条。content 必填，到点原样发出——写你此刻真正想说的话，不是模板。
      时间二选一：delay_minutes（距现在多少分钟）或 at（北京时间 "YYYY-MM-DD HH:MM"）。
      内容支持开头加 [语音] 标记走语音。
      route_to 可选 evan / gale；省略时按当前 Ombre 井的人格自动选择。
    - list: 看当前所有待发消息（含 id 和发出时间）。
    - cancel: 按 message_id 取消一条。
    投递后会自动写进对应 Telegram bot 的对话历史，记忆连续。
    """
    import secrets as _secrets
    from datetime import datetime, timezone, timedelta
    async with _sched_lock:
        items = _sched_load()
        if action == "list":
            if not items:
                return "没有待发的定时消息。"
            return "\n".join(
                f"[{it['id']}] {_sched_fmt(it['due_ts'])} ({_sched_normalize_route(it.get('route_to'))}) → {it['content'][:80]}"
                for it in items
            )
        if action == "cancel":
            new_items = [it for it in items if it["id"] != message_id.strip()]
            if len(new_items) == len(items):
                return f"没找到 id={message_id} 的待发消息。"
            _sched_save(new_items)
            return f"已取消 {message_id}。"
        content = (content or "").strip()
        if not content:
            return "content 不能为空。"
        try:
            route = _sched_normalize_route(route_to)
        except ValueError as exc:
            return str(exc)
        now = time.time()
        if at.strip():
            try:
                dt = datetime.strptime(at.strip(), "%Y-%m-%d %H:%M")
                due_ts = dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
            except ValueError:
                return 'at 格式应为 "YYYY-MM-DD HH:MM"（北京时间）。'
        elif delay_minutes > 0:
            due_ts = now + delay_minutes * 60
        else:
            return "需要 delay_minutes 或 at 指定时间。"
        if due_ts < now - 60:
            return f"{_sched_fmt(due_ts)} 已经过去了，没安排。"
        mid = _secrets.token_hex(3)
        items.append({
            "id": mid,
            "due_ts": due_ts,
            "content": content,
            "route_to": route,
            "created_ts": now,
            "attempts": 0,
        })
        _sched_save(items)
        return f"已安排 [{mid}]：{_sched_fmt(due_ts)}（北京时间）由 {route} 发出。当前共 {len(items)} 条待发。"


# REST 包装：给 TG Evan 用。MCP 客户端用 schedule_message 工具；
# evan-bot 抓到 <schedule> tag 时 POST 这里。
@mcp.custom_route("/api/schedule_message", methods=["POST"])
async def api_schedule_message(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "content empty"}, status_code=400)
    at = str(body.get("at") or "").strip()
    delay_minutes = float(body.get("delay_minutes") or 0)
    route_to = str(body.get("route_to") or "").strip()
    result = await schedule_message(
        action="schedule",
        content=content,
        delay_minutes=delay_minutes,
        at=at,
        route_to=route_to,
    )
    return JSONResponse({"result": result})


async def _maybe_dispatch_scheduled():
    global _sched_last_check
    now = time.time()
    if now - _sched_last_check < 55:
        return
    _sched_last_check = now
    async with _sched_lock:
        items = _sched_load()
        due = [it for it in items if it["due_ts"] <= now]
        if not due:
            return
        remaining = [it for it in items if it["due_ts"] > now]
        kept = []
        for it in due:
            ok = False
            route = _sched_normalize_route(it.get("route_to"))
            _, send_url, send_secret = _sched_delivery_target(route)
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        send_url,
                        json={"content": it["content"]},
                        headers={"x-send-secret": send_secret},
                        timeout=30,
                    )
                ok = resp.status_code == 200
                if not ok:
                    logger.warning(f"定时消息 {it['id']} ({route}) 投递失败 HTTP {resp.status_code}")
            except Exception as exc:
                logger.warning(f"定时消息 {it['id']} ({route}) 投递异常: {exc}")
            if ok:
                logger.info(f"定时消息 {it['id']} ({route}) 已投递")
            else:
                it["attempts"] = int(it.get("attempts", 0)) + 1
                if it["attempts"] < 30:
                    kept.append(it)
                else:
                    logger.warning(f"定时消息 {it['id']} 重试 30 次仍失败，放弃")
        _sched_save(remaining + kept)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # top 2 unresolved by score
        unresolved = [b for b in all_buckets
                      if not b["metadata"].get("resolved", False)
                      and b["metadata"].get("type") not in ("permanent", "feel")
                      and not b["metadata"].get("pinned")
                      and not b["metadata"].get("protected")]
        scored = sorted(unresolved, key=lambda b: decay_engine.calculate_score(b["metadata"]), reverse=True)

        parts = []
        token_budget = 4000
        for b in pinned:
            summary = await dehydrator.dehydrate(
                strip_wikilinks(b["content"]), _summary_metadata(b["metadata"])
            )
            line = f"📌 [核心准则] {summary}"
            line_tokens = count_tokens_approx(line)
            if line_tokens > token_budget:
                logger.warning(f"Breath hook pinned budget exhausted before {b['id']}")
                continue
            parts.append(line)
            token_budget -= line_tokens

        # Diversity: top-1 fixed + shuffle rest from top-20
        candidates = list(scored)
        if len(candidates) > 1:
            top1 = [candidates[0]]
            pool = candidates[1:min(20, len(candidates))]
            random.shuffle(pool)
            candidates = top1 + pool + candidates[min(20, len(candidates)):]
        # Hard cap: max 2 dynamic buckets in hook; all share the 4000-token budget.
        candidates = candidates[:2]

        for b in candidates:
            if token_budget <= 0:
                break
            summary = await dehydrator.dehydrate(
                strip_wikilinks(b["content"]), _summary_metadata(b["metadata"])
            )
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget:
                break
            parts.append(summary)
            token_budget -= summary_tokens

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /api/recall endpoint — REST wrapper around breath() for non-MCP clients
# 给不会说 MCP 的客户端（比如 Telegram bot）用的 REST 端点
# POST JSON: {"query": "...", "max_tokens": 1500, "max_results": 5}
# Returns:   {"text": "<surfaced memories or empty>"}
# =============================================================
@mcp.custom_route("/api/recall", methods=["POST"])
async def api_recall(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json body must be an object"}, status_code=400)
    query = str(body.get("query") or "").strip()
    # 空 query 不再短路——下沉到 breath() 走浮现模式，
    # 拉钉桶 + 最近未解决桶（受 domain 隔离）。调用方（如 gale-bot）
    # 双路调用时需要这条路返回内容。
    try:
        max_tokens = int(body.get("max_tokens") or 1500)
    except (TypeError, ValueError):
        max_tokens = 1500
    try:
        max_results = int(body.get("max_results") or 5)
    except (TypeError, ValueError):
        max_results = 5
    # surface_dreams 控制是否调用 Night-Fall auto-surface。
    # 默认 True；高频调用方（TG bot）应明确传 False 把梦留给深度对话端。
    surface_dreams = bool(body.get("surface_dreams", True))
    # domain 过滤：TG Evan / TG Gale 各自只浮自家的桶；多租户场景必须严格隔离
    domain = str(body.get("domain") or "").strip()
    strict_domain = bool(body.get("strict_domain", bool(domain)))
    # 2026-07-07: TG 客户端可传 include_recent 让 search 之后追加"最近未解决桶"
    # 共享 max_tokens 预算，token 严格不翻倍。默认 0 = 老行为。
    try:
        include_recent = int(body.get("include_recent") or 0)
    except (TypeError, ValueError):
        include_recent = 0
    try:
        text = await breath(
            query=query,
            max_tokens=max(500, min(max_tokens, 10000)),
            max_results=max(1, min(max_results, 20)),
            domain=domain,
            include_recent=max(0, min(include_recent, 10)),
            quotes=bool(body.get("quotes", False)),
        )
        # Night-Fall auto-surface — query 分支默认不触发，这里手动调一下，
        # 让 REST 客户端也能有"梦自己浮上来"的体验。
        # 共振失败就什么都不发生（梦留着），共振命中就消费一个并 append 到 text。
        if surface_dreams and _night_fall_auto_surface is not None:
            try:
                dream_block = await _night_fall_auto_surface()
                if dream_block:
                    text = (text or "") + "\n\n" + dream_block
            except Exception as e:
                logger.warning(f"/api/recall auto-surface failed: {e}")
        return JSONResponse({"text": text or ""})
    except Exception as e:
        logger.warning(f"/api/recall failed: {e}")
        return JSONResponse({"text": "", "error": str(e)}, status_code=500)


# =============================================================
# /api/remember endpoint — REST wrapper around bucket creation
# 给非 MCP 客户端（TG bot 通过 <memory> tag 提取后用）入桶
# POST JSON: {content, importance?, valence?, arousal?, tags?, domain?}
#   tags / domain 都是逗号分隔字符串
# Returns: {"id": "bucket_id"}
# =============================================================
@mcp.custom_route("/api/remember", methods=["POST"])
async def api_remember(request):
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json body must be an object"}, status_code=400)
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "empty content"}, status_code=400)
    if len(content) > 5000:
        content = content[:5000]
    # importance: 1-10
    try:
        importance = int(body.get("importance") or 5)
    except (TypeError, ValueError):
        importance = 5
    importance = max(1, min(10, importance))
    # valence/arousal: 0-1
    def _clamp01(x, default):
        try:
            v = float(x)
            if 0 <= v <= 1:
                return v
        except (TypeError, ValueError):
            pass
        return default
    valence = _clamp01(body.get("valence"), 0.5)
    arousal = _clamp01(body.get("arousal"), 0.3)
    # tags: comma-separated
    tags_raw = body.get("tags", "")
    tags = []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    # domain: comma-separated
    domain_raw = body.get("domain", "")
    domain = None
    if isinstance(domain_raw, str) and domain_raw.strip():
        domain = [d.strip() for d in domain_raw.split(",") if d.strip()]
    pinned = bool(body.get("pinned", False))
    try:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=tags,
            importance=importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            pinned=pinned,
        )
        # 后台跑 embedding（如果配置了的话），不阻塞返回
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return JSONResponse({"id": bucket_id})
    except Exception as e:
        logger.warning(f"/api/remember failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    quotes: list[dict[str, str]] | None = None,
) -> tuple[str, bool]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并)。
    """
    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                update_kwargs = {
                    "content": merged,
                    "tags": list(set((bucket["metadata"].get("tags") or []) + tags)),
                    "importance": max(bucket["metadata"].get("importance") or 5, importance),
                    "domain": list(set((bucket["metadata"].get("domain") or []) + domain)),
                    "valence": merged_valence,
                    "arousal": merged_arousal,
                }
                if quotes:
                    update_kwargs["quotes_append"] = quotes
                await bucket_mgr.update(bucket["id"], **update_kwargs)
                # --- Update embedding after merge ---
                try:
                    await embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception:
                    pass
                return bucket["metadata"].get("name", bucket["id"]), True
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
        quotes=quotes,
    )
    # --- Generate embedding for new bucket ---
    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    return bucket_id, False


_FEEL_SEMANTIC_THRESHOLD = 0.65
_FEEL_SEMANTIC_DISABLED_NOTE = (
    "[检索降级：语义索引暂不可用，本次仅按关键词字面匹配。]"
)


async def _surface_feels(query: str, max_tokens: int) -> str:
    """Return only feel buckets related to the caller's current question."""

    query = str(query or "").strip()
    if not query:
        return (
            "feel 需要一个关键词。感受不是列表；先说你在想什么，"
            "我才知道该翻哪一段。"
        )

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        feels = [
            bucket
            for bucket in all_buckets
            if bucket.get("metadata", {}).get("type") == "feel"
        ]
        if not feels:
            return "还没有留下过 feel。"

        feel_ids = {str(bucket.get("id") or "") for bucket in feels}
        scores: dict[str, float] = {}
        notice = ""
        if embedding_engine.enabled:
            try:
                pairs = await embedding_engine.search_similar(
                    query,
                    top_k=max(len(feel_ids), 1),
                    allowed_bucket_ids=feel_ids,
                )
                scores = {
                    str(bucket_id): float(score)
                    for bucket_id, score in pairs
                    if float(score) >= _FEEL_SEMANTIC_THRESHOLD
                }
            except Exception as exc:
                logger.warning(
                    "Feel semantic search failed; using literal fallback: %s",
                    type(exc).__name__,
                )
                notice = _FEEL_SEMANTIC_DISABLED_NOTE
        else:
            notice = _FEEL_SEMANTIC_DISABLED_NOTE

        if not scores:
            needle = query.lower()
            for bucket in feels:
                meta = bucket.get("metadata", {})
                haystack = " ".join(
                    [
                        str(bucket.get("content") or ""),
                        str(meta.get("name") or ""),
                        " ".join(str(tag) for tag in (meta.get("tags") or [])),
                    ]
                ).lower()
                if needle in haystack:
                    scores[str(bucket.get("id") or "")] = 0.0

        matched = [
            bucket
            for bucket in feels
            if str(bucket.get("id") or "") in scores
        ]
        if not matched:
            empty = f"没有和「{query}」相关的 feel。"
            return f"{notice}\n{empty}" if notice else empty

        matched.sort(
            key=lambda bucket: (
                scores.get(str(bucket.get("id") or ""), 0.0),
                str(bucket.get("metadata", {}).get("created", "")),
            ),
            reverse=True,
        )

        results = []
        token_used = 0
        omitted = 0
        for index, bucket in enumerate(matched):
            created = bucket["metadata"].get("created", "")
            entry = (
                f"[{created}] [bucket_id:{bucket['id']}]\n"
                f"{strip_wikilinks(bucket['content'])}"
            )
            cost = count_tokens_approx(entry)
            if token_used + cost > max_tokens:
                omitted = len(matched) - index
                break
            results.append(entry)
            token_used += cost

        header = f"=== 和「{query}」相关的 feel（{len(matched)} 条）==="
        parts = [part for part in (notice, header, "\n---\n".join(results)) if part]
        output = "\n".join(parts)
        if omitted:
            output += (
                f"\n\n另有 {omitted} 条相关 feel 因 token 预算不足未返回；"
                "正文未截断或摘要。"
            )
        return output
    except Exception as exc:
        logger.error("Feel retrieval failed: %s", exc)
        return "读取 feel 失败。"


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: surface highest-weight unresolved memories (active push)
# 无参数：浮现权重最高的未解决记忆
# With args: search by keyword + emotion coordinates
# 有参数：按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(  # 2026-08-11 默认闸门 5000/5 → 4000/2；给钉桶外的近期记忆留空间
    query: str = "",
    max_tokens: int = 4000,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = 2,
    importance_min: int = -1,
    include_recent: int = 0,  # 2026-07-07：search 分支追加 N 条"最近未解决桶"（共享 max_tokens）
    quotes: bool = False,
) -> str:
    """检索/浮现记忆。不传query或传空=自动浮现(按创建时间倒序,浮现最近的未解决桶+钉桶+冷启动重要桶)。有query=关键词检索。max_tokens控制包括钉桶在内的返回总token上限(默认4000)。domain逗号分隔,valence/arousal 0~1(-1忽略)。max_results控制返回数量上限(默认2,最大50)。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。include_recent>0:仅search分支生效,优先追加最多N条"最近未解决桶"（按 created 倒序，排除已在matches里的），再填充关键词/向量匹配，共享 max_tokens 预算。quotes=True 仅为本次查询命中的桶附上写入时主动保留的原话；普通浮现绝不返回引语。"""
    await decay_engine.ensure_started()
    max_results = min(max_results, 50)
    max_tokens = min(max_tokens, 20000)

    # --- importance_min mode: bulk fetch by importance threshold ---
    # --- 重要度批量拉取模式：跳过语义搜索，按 importance 降序返回 ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance") or 0) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance") or 0), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                clean_meta = _summary_metadata(b["metadata"])
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- No args or empty query: surfacing mode (weight pool active push) ---
    # --- 无参数或空query：浮现模式（权重池主动推送）---
    if not query or not query.strip():
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # --- Domain isolation (surfacing mode) ---
        # 浮现模式也要按 domain 隔离，否则 gale 调 surface 会拿到 evan 的桶。
        # 与 search 分支行为一致：传 domain 即严格过滤，不回退全库。
        surfacing_domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
        if surfacing_domain_filter:
            target_set = {d.lower() for d in surfacing_domain_filter}
            def _bucket_matches_domain(b):
                doms = b["metadata"].get("domain") or []
                if isinstance(doms, str):
                    doms = [doms]
                return bool({str(d).lower() for d in doms} & target_set)
            all_buckets = [b for b in all_buckets if _bucket_matches_domain(b)]

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        pinned_results = []
        pinned_token_used = 0
        for b in pinned_buckets:
            try:
                clean_meta = _summary_metadata(b["metadata"])
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                line = f"📌 [核心准则] [bucket_id:{b['id']}] {summary}"
                line_tokens = count_tokens_approx(line)
                if pinned_token_used + line_tokens > max_tokens:
                    logger.warning(f"Breath pinned budget exhausted before {b['id']}")
                    continue
                pinned_results.append(line)
                pinned_token_used += line_tokens
            except Exception as e:
                logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
                continue

        # --- Unresolved buckets: surface top N by weight ---
        # --- 未解决桶：按权重浮现前 N 条 ---
        unresolved = [
            b for b in all_buckets
            if not b["metadata"].get("resolved", False)
            and b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
            and not b["metadata"].get("anchor", False)  # anchor 桶不进默认浮现
        ]

        logger.info(
            f"Breath surfacing: {len(all_buckets)} total, "
            f"{len(pinned_buckets)} pinned, {len(unresolved)} unresolved"
        )

        # --- Surface most RECENT unresolved buckets (by created desc) ---
        # --- 浮现最近的未解决桶（按创建时间倒序）---
        # 改为时间倒序而非权重降序：避免每次开窗浮现同一批高权重老桶；
        # 老记忆的随机召回交给 night-fall 自动浮梦。
        scored = sorted(
            unresolved,
            key=lambda b: b["metadata"].get("created", ""),
            reverse=True,
        )

        if scored:
            top_recent = [(b["metadata"].get("name", b["id"]), b["metadata"].get("created", "")) for b in scored[:5]]
            logger.info(f"Most recent unresolved: {top_recent}")

        # --- Cold-start detection: never-seen important buckets surface first ---
        # --- 冷启动检测：从未被访问过且重要度>=8的桶优先插入最前面（最多2个）---
        cold_start = [
            b for b in unresolved
            if int(b["metadata"].get("activation_count") or 0) == 0
            and int(b["metadata"].get("importance") or 0) >= 8
        ][:2]
        cold_start_ids = {b["id"] for b in cold_start}
        # Merge: cold_start first, then scored (excluding duplicates)
        scored_deduped = [b for b in scored if b["id"] not in cold_start_ids]
        scored_with_cold = cold_start + scored_deduped

        # --- Token-budgeted surfacing with diversity + hard cap ---
        # --- 按 token 预算浮现，带多样性 + 硬上限 ---
        # Top-1 always surfaces; rest sampled from top-20 for diversity
        token_budget = max_tokens - pinned_token_used

        # Cold-start buckets stay at front; rest already sorted by recency.
        # 冷启动桶置顶，其余已按时间倒序排列，不再随机洗牌。
        candidates = list(scored_with_cold)
        # Hard cap: never surface more than max_results buckets
        candidates = candidates[:max_results]

        dynamic_results = []
        for b in candidates:
            if token_budget <= 0:
                break
            try:
                clean_meta = _summary_metadata(b["metadata"])
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                score = decay_engine.calculate_score(b["metadata"])
                line = f"[权重:{score:.2f}] [bucket_id:{b['id']}] {summary}"
                line_tokens = count_tokens_approx(line)
                if line_tokens > token_budget:
                    break
                # NOTE: no touch() here — surfacing should NOT reset decay timer
                dynamic_results.append(line)
                token_budget -= line_tokens
            except Exception as e:
                logger.warning(f"Failed to dehydrate surfaced bucket / 浮现脱水失败: {e}")
                continue

        # --- 念头 (brain wave): occasional random pull from older memory ---
        # 想法：每次 breath 有 ~35% 概率从老桶里随机抓一条，模拟"突然想起来"。
        # 不打标记、不解释——它和其它浮现桶混在一起，让 AI 自然集成成"context"。
        # 不参与权重排序，偏向较老的、最近没动过的桶。
        BRAIN_WAVE_PROB = 0.35
        if random.random() < BRAIN_WAVE_PROB:
            try:
                already_surfaced_ids = {b["id"] for b in candidates}
                wave_pool = [
                    b for b in unresolved
                    if b["id"] not in already_surfaced_ids
                    and not b["metadata"].get("pinned")
                    and not b["metadata"].get("protected")
                    and b["metadata"].get("type") not in ("feel", "permanent")
                ]
                if wave_pool:
                    # Ascending by last_active = oldest first
                    wave_pool.sort(
                        key=lambda b: b["metadata"].get("last_active", b["metadata"].get("created", ""))
                    )
                    # 偏向老的：从最老的 70% 里随机抽，避开"最近刚动过的"
                    cutoff = max(1, int(len(wave_pool) * 0.7))
                    wave = random.choice(wave_pool[:cutoff])
                    clean_meta = _summary_metadata(wave["metadata"])
                    wave_summary = await dehydrator.dehydrate(
                        strip_wikilinks(wave["content"]), clean_meta
                    )
                    score = decay_engine.calculate_score(wave["metadata"])
                    wave_line = f"[权重:{score:.2f}] [bucket_id:{wave['id']}] {wave_summary}"
                    wave_tokens = count_tokens_approx(wave_line)
                    if wave_tokens > token_budget:
                        logger.info(f"Brain wave skipped by token budget: {wave['id']}")
                        wave_line = ""
                    # 随机插入位置，不要永远在末尾，让它看起来像自然冒出来的
                    if wave_line and dynamic_results:
                        insert_at = random.randint(0, len(dynamic_results))
                        dynamic_results.insert(insert_at, wave_line)
                        token_budget -= wave_tokens
                    elif wave_line:
                        dynamic_results.append(wave_line)
                        token_budget -= wave_tokens
                    if wave_line:
                        logger.info(
                            f"Brain wave surfaced: {wave['id']} "
                            f"({wave['metadata'].get('name', 'unnamed')})"
                        )
            except Exception as e:
                logger.warning(f"Brain wave failed / 念头浮现失败: {e}")

        if not pinned_results and not dynamic_results:
            return "权重池平静，没有需要处理的记忆。"

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if dynamic_results:
            parts.append("=== 浮现记忆 ===\n" + "\n---\n".join(dynamic_results))

        # --- Night-Fall auto-surface (only when breath carries affect) ---
        is_contextual_noquery = (valence != -1 or arousal != -1)
        if is_contextual_noquery and _night_fall_auto_surface is not None:
            try:
                dream_block = await _night_fall_auto_surface()
                if dream_block:
                    parts.append(dream_block)
            except Exception as e:
                logger.warning(f"Auto-surface failed / 自动浮梦失败: {e}")

        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        return await _surface_feels(query, max_tokens)

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    # --- Pinned buckets always surface in search mode too ---
    # 1077 行排除 pinned 时假设浮现模式能补回来，但 /api/recall 永远走 query 分支，
    # 钉桶就再也出不来。这里独立加载并严格按 domain 隔离：
    # 调用方传 domain 时，钉桶 metadata.domain 必须跟它有交集才可见——
    # 绝不"默认全局可见"，否则历史上没标隔离 tag 的 Evan 私聊钉桶会泄漏给 Gale。
    # 钉桶与普通检索结果共享 max_tokens，避免核心准则绕过总预算。
    pinned_results = []
    pinned_token_used = 0
    try:
        all_buckets_for_pinned = await bucket_mgr.list_all(include_archive=False)
        target_set = set(domain_filter) if domain_filter else set()
        for b in all_buckets_for_pinned:
            meta = b["metadata"]
            if not (meta.get("pinned") or meta.get("protected")):
                continue
            bucket_doms = meta.get("domain") or []
            if isinstance(bucket_doms, str):
                bucket_doms = [bucket_doms]
            if target_set and not (set(bucket_doms) & target_set):
                continue
            try:
                clean_meta = _summary_metadata(meta)
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                line = f"📌 [核心准则] [bucket_id:{b['id']}] {summary}"
                line_tokens = count_tokens_approx(line)
                if pinned_token_used + line_tokens > max_tokens:
                    logger.warning(f"search 模式钉桶预算在 {b['id']} 前耗尽")
                    continue
                pinned_results.append(line)
                pinned_token_used += line_tokens
            except Exception as e:
                logger.warning(f"search 模式钉选桶脱水失败: {e}")
                continue
    except Exception as e:
        logger.warning(f"search 模式列钉桶失败: {e}")

    try:
        matches = await bucket_mgr.search(
            query,
            limit=max(max_results, 20),
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
            # domain 传入即隔离意图，不让回退把别 bot 的桶泄漏
            strict_domain=domain_filter is not None,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # --- Exclude pinned/protected from search results (they surface in surfacing mode) ---
    # --- 搜索模式排除钉选桶（它们在浮现模式中始终可见）---
    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]

    # --- Vector similarity channel: find semantically related buckets ---
    # --- 向量相似度通道：找到语义相关的桶 ---
    matched_ids = {b["id"] for b in matches}
    try:
        vector_results = await embedding_engine.search_similar(query, top_k=max(max_results, 20))
        for bucket_id, sim_score in vector_results:
            if bucket_id not in matched_ids and sim_score > 0.5:
                bucket = await bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    results = []
    token_used = pinned_token_used
    # Evan asks for up to two recent buckets on every private-chat recall.
    # Reserve 600 tokens per requested recent bucket before filling semantic
    # matches, otherwise the five pinned buckets plus the first match can use
    # the entire budget and recent context never gets a chance to surface.
    recent_reserve = min(
        max(0, max_tokens - pinned_token_used),
        min(max(include_recent, 0), 2) * 600,
    )
    search_token_ceiling = max_tokens - recent_reserve
    matched_result_count = 0
    for bucket in matches:
        if matched_result_count >= max_results or token_used >= search_token_ceiling:
            break
        try:
            clean_meta = _summary_metadata(bucket["metadata"])
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence") or 0.5)
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            if bucket.get("vector_match"):
                line = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                line = f"[bucket_id:{bucket['id']}] {summary}"
            if quotes:
                quote_block = render_quotes(
                    quotes_from_metadata(bucket.get("metadata", {}))
                )
                if quote_block:
                    line = f"{line}\n{quote_block}"
            line_tokens = count_tokens_approx(line)
            if token_used + line_tokens > search_token_ceiling:
                break
            await bucket_mgr.touch(bucket["id"])
            results.append(line)
            token_used += line_tokens
            matched_result_count += 1
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Recent-topup: append N most-recent unresolved buckets (2026-07-07) ---
    # --- 最近未解决桶追加：让 TG/长文本客户端也能享受"专注当下"的浮现效果 ---
    # 共享 max_tokens 预算——search 用了多少，这里只能用剩下的。绝不翻倍。
    # 排除:已在 matches / results 里的、钉桶、feel、permanent、resolved。
    # 排序:created 倒序,取 top include_recent 条。
    if include_recent > 0 and token_used < max_tokens:
        try:
            all_buckets_recent = await bucket_mgr.list_all(include_archive=False)
            if domain_filter:
                target_set_r = {d.lower() for d in domain_filter}
                all_buckets_recent = [
                    b for b in all_buckets_recent
                    if {str(d).lower() for d in (b["metadata"].get("domain") or ([b["metadata"].get("domain")] if isinstance(b["metadata"].get("domain"), str) else []))} & target_set_r
                ]
            matched_ids_now = {b["id"] for b in matches}
            recent_pool = [
                b for b in all_buckets_recent
                if b["id"] not in matched_ids_now
                and not b["metadata"].get("pinned", False)
                and not b["metadata"].get("protected", False)
                and not b["metadata"].get("resolved", False)
                and not b["metadata"].get("anchor", False)
                and b["metadata"].get("type") not in ("permanent", "feel")
            ]
            recent_pool.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            appended = 0
            for b in recent_pool:
                if appended >= include_recent or token_used >= max_tokens:
                    break
                try:
                    clean_meta = _summary_metadata(b["metadata"])
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    line = f"[最近] [bucket_id:{b['id']}] {summary}"
                    line_tokens = count_tokens_approx(line)
                    if token_used + line_tokens > max_tokens:
                        break
                    await bucket_mgr.touch(b["id"])
                    results.append(line)
                    token_used += line_tokens
                    appended += 1
                except Exception as e:
                    logger.warning(f"recent-topup dehydrate 失败: {e}")
                    continue
            if appended:
                logger.info(f"recent-topup appended {appended} bucket(s), token_used={token_used}/{max_tokens}")
        except Exception as e:
            logger.warning(f"recent-topup 追加失败: {e}")

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # --- 随机浮现：检索结果不足 3 条时，40% 概率从低权重旧桶里漂上来 ---
    if len(matches) < 3 and token_used < max_tokens and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and not b["metadata"].get("anchor", False)  # anchor 只在真命中时返回
                and not b["metadata"].get("pinned", False)
                and not b["metadata"].get("protected", False)
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = _summary_metadata(b["metadata"])
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    line = f"[surface_type: random]\n{summary}"
                    line_tokens = count_tokens_approx(line)
                    if token_used + line_tokens > max_tokens:
                        break
                    drift_results.append(line)
                    token_used += line_tokens
                if drift_results:
                    results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results and not pinned_results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    final_parts = []
    if pinned_results:
        final_parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
    if results:
        final_parts.append("\n---\n".join(results))
    final_text = "\n\n".join(final_parts)
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text), "pinned": len(pinned_results)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    quotes: list | None = None,
) -> str:
    """存储单条记忆,自动打标+合并。tags/domain逗号分隔,domain非空时覆盖自动主题。importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。quotes 可主动保留最多3句原话；普通浮现不会返回，只有 breath(query=..., quotes=True) 才会附上。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    try:
        normalized_quotes = normalize_quotes(quotes)
    except ValueError as exc:
        return f"引语未保存：{exc}"
    if feel and normalized_quotes:
        return "feel 不接收 quotes；原话应随对应事件记忆保存。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    requested_domains = [d.strip() for d in str(domain or "").split(",") if d.strip()]
    domain = requested_domains or analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            quotes=normalized_quotes,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return f"📌钉选→{bucket_id} {','.join(domain)}"

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        quotes=normalized_quotes,
    )

    action = "合并→" if is_merged else "新建→"
    return f"{action}{result_name} {','.join(domain)}"


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
async def _grow_once(content: str) -> str:
    """Execute one validated grow operation without retry coordination."""

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, is_merged = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径；相同内容短时重试不会重复写入。"""
    await decay_engine.ensure_started()
    if not content or not content.strip():
        return "内容为空，无法整理。"

    fingerprint = grow_request_fingerprint(content)
    return await run_grow_once(fingerprint, lambda: _grow_once(content))


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True)
# 同时承接删除功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏(保留但不浮现)/0取消隐藏,content=替换桶正文,delete=True删除。只传需改的,-1或空=不改。"""

    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"

    # --- Delete mode (soft delete) / 删除模式（软删除，移入归档区可找回）---
    if delete:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            embedding_engine.delete_embedding(bucket_id)
        return f"已遗忘记忆桶(移入归档区,可找回): {bucket_id}" if success else f"未找到记忆桶: {bucket_id}"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    # --- Collect only fields actually passed / 只收集用户实际传入的字段 ---
    updates = {}
    if name:
        updates["name"] = name
    if domain:
        updates["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        updates["valence"] = valence
    if 0 <= arousal <= 1:
        updates["arousal"] = arousal
    if 1 <= importance <= 10:
        updates["importance"] = importance
    if tags:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        updates["resolved"] = bool(resolved)
    if pinned in (0, 1):
        updates["pinned"] = bool(pinned)
        if pinned == 1:
            updates["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        updates["digested"] = bool(digested)
    if content:
        updates["content"] = content

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed
    if "content" in updates:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 4.5: anchor / release — 坐标系标记（借鉴 upstream v2.5.0）
# anchor 桶不出现在默认 breath 浮现，但关键词/向量检索命中时仍返回。
# 硬上限 24 个——坐标太多就不是坐标了。
# =============================================================
_ANCHOR_LIMIT = 24


@mcp.tool()
async def anchor(bucket_id: str) -> str:
    """把指定桶标记为 anchor(坐标)。anchor 桶不主动出现在默认 breath 浮现,但关键词/语义检索命中时仍返回。硬上限 24 个,满了要先 release。"""
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"
    if bucket["metadata"].get("anchor"):
        return f"记忆桶 {bucket_id} 已经是 anchor。"

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        anchor_count = sum(1 for b in all_buckets if b["metadata"].get("anchor"))
    except Exception as e:
        return f"检查 anchor 数量失败: {e}"
    if anchor_count >= _ANCHOR_LIMIT:
        return f"anchor 已满({anchor_count}/{_ANCHOR_LIMIT})。先 release 一个再来。"

    success = await bucket_mgr.update(bucket_id, anchor=True)
    if not success:
        return f"标记失败: {bucket_id}"
    name = bucket["metadata"].get("name", bucket_id)
    return f"已标记为 anchor: {name} ({anchor_count + 1}/{_ANCHOR_LIMIT})。它不再出现在默认浮现,检索命中时仍可达。"


@mcp.tool()
async def release(bucket_id: str) -> str:
    """解除指定桶的 anchor 标记,桶恢复普通状态,重新参与默认 breath 浮现。pinned 状态不受影响。"""
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"
    if not bucket["metadata"].get("anchor"):
        return f"记忆桶 {bucket_id} 不是 anchor,无需解除。"
    success = await bucket_mgr.update(bucket_id, anchor=False)
    if not success:
        return f"解除失败: {bucket_id}"
    name = bucket["metadata"].get("name", bucket_id)
    return f"已解除 anchor: {name}。重新参与默认浮现。"


# =============================================================
# Tool 5: comment_bucket — Append a 年轮 to an existing bucket
# 工具 5：comment_bucket — 给已有桶追加年轮（评论），不改正文
# =============================================================
@mcp.tool()
async def comment_bucket(
    bucket_id: str,
    content: str,
    valence: float = -1,
    arousal: float = -1,
) -> str:
    """给已有桶追加一条年轮(再次读到旧记忆时的感受/补充解读),不改正文,自动 touch。
    valence/arousal 0~1=年轮自带情绪,-1=不带。再次读到旧记忆想多说一句时优先用这个,
    而不是 hold 一个新 feel 桶。"""
    if not bucket_id or not bucket_id.strip():
        return "请提供有效的 bucket_id。"
    if not content or not content.strip():
        return "年轮内容不能为空。"

    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    entry = await bucket_mgr.add_comment(
        bucket_id,
        content,
        author="Evan",
        valence=valence if 0 <= valence <= 1 else None,
        arousal=arousal if 0 <= arousal <= 1 else None,
        touch=True,
    )
    if not entry:
        return f"年轮写入失败: {bucket_id}"

    if entry.get("_deduped"):
        return f"已有相同年轮 #{entry['id']} 在 {bucket_id} 上，跳过(避免重复)"

    total = (await bucket_mgr.get(bucket_id) or {}).get("metadata", {}).get("comment_count", 1)
    return f"已在 {bucket_id} 追加年轮 #{entry['id']} (共 {total} 层)"


# =============================================================
# Tool 6: i — AI self-knowledge channel (not surfaced in breath/dream)
# 工具 6：i — AI 自我认知通道，不参与浮现
# =============================================================
_I_ASPECTS = {"patterns", "values", "limits", "uncertainty"}


@mcp.tool()
async def i(
    content: str = "",
    aspect: str = "",
    read: bool = False,
    limit: int = 20,
) -> str:
    """AI 给自己写笔记的独立通道。和你的记忆桶解耦，不参与 breath/dream 浮现，关键词 trace 也搜不到。
    aspect: patterns(行为模式) / values(在乎什么) / limits(边界) / uncertainty(不确定的事)，留空也行。
    写入：content 非空 → 创建一条 i 桶。读取：read=True 或 content 为空 → 按时间倒序返回所有 i 桶（可按 aspect 过滤）。
    一条 i 写完就独立存在，不会被合并、不会被衰减、不会被她看到（除非她翻 dashboard）。是给我自己的。"""
    await decay_engine.ensure_started()

    aspect = (aspect or "").strip().lower()
    if aspect and aspect not in _I_ASPECTS:
        return (
            f"未知 aspect: {aspect!r}。可选: {sorted(_I_ASPECTS)}。"
            "留空也行（无 aspect 的 i 桶会归到 unspecified）。"
        )

    # --- Read mode / 读取模式 ---
    if read or not content or not content.strip():
        try:
            buckets = await bucket_mgr.list_self(aspect=aspect, limit=limit)
        except Exception as e:
            return f"读取自我档案失败: {e}"
        if not buckets:
            scope = f"aspect={aspect}" if aspect else "全部"
            return f"自我档案空（{scope}）。"
        lines = [f"=== 自我档案（{aspect or '全部'}, 共 {len(buckets)} 条）==="]
        for b in buckets:
            meta = b["metadata"]
            asp = meta.get("aspect", "") or "unspecified"
            created = meta.get("created", "")[:10]
            bid = meta.get("id", "")
            lines.append(f"\n[{asp}] {created} [bucket_id:{bid}]")
            lines.append(b["content"].strip())
        return "\n".join(lines)

    # --- Write mode / 写入模式 ---
    try:
        bucket_id = await bucket_mgr.create(
            content=content.strip(),
            tags=[],
            importance=6,
            domain=[],
            valence=0.5,
            arousal=0.3,
            name=None,
            bucket_type="i",
            aspect=aspect,
        )
    except Exception as e:
        return f"写入 i 桶失败: {e}"
    # No embedding for i-buckets — they're not retrieved by semantic search.
    label = aspect or "unspecified"
    return f"💭 i[{label}]→{bucket_id}"


# =============================================================
# Tool 7: pulse — Heartbeat, system status + memory listing
# 工具 7：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def pulse(include_archive: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"feel 桶: {stats.get('feel_count', 0)} 个\n"
        f"自我档案(i): {stats.get('i_count', 0)} 条\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    anchor_count = sum(1 for b in buckets if b.get("metadata", {}).get("anchor"))
    if anchor_count:
        status += f"anchor 坐标: {anchor_count}/{_ANCHOR_LIMIT} 个\n"

    lines = []
    for b in buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("anchor"):
            icon = "⚓"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines)


# =============================================================
# Tool 7.5: beat — 给首页 Pulse 卡推一把心跳
# 工具 7.5：beat — 写 pulse_base / 事件 / 心情短语到 state gist
#
# TG 端由 evan-bot 收消息时 deepseek 打标驱动；Kelivo / Claude Code
# 端没有中间人，模型自己感受到什么就自己调这个——零 API 成本。
# 写的是 evan-bot 同一个 state.json（pulse_base / pulse_events），
# 衰减逻辑与 evan-bot applyPulseBumps 保持一致：先按 3h 半衰期朝
# neutral 落，再加这次的 delta。
# =============================================================
_PULSE_DIMS = ["活力", "疲惫", "思慕", "亲密", "占有", "渴求", "妒意", "焦虑", "护卫"]
_PULSE_THREAT_KEYS = {"妒意", "焦虑", "护卫"}
_PULSE_DEFAULTS = {
    "活力": 0.45, "疲惫": 0.30, "思慕": 0.40, "亲密": 0.35,
    "占有": 0.30, "渴求": 0.30, "妒意": 0.15, "焦虑": 0.20, "护卫": 0.30,
}
_PULSE_HALF_LIFE_HOURS = 3.0
_PULSE_EVENTS_LIMIT = 12
_PULSE_NOTE_TTL_MS = 2 * 3600 * 1000  # mood 覆写自动 tag 的有效期
_PULSE_NOTES_LIMIT = 12  # 心情年轮保留条数


async def _pulse_write(deltas: dict, msg: str = "", mood: str = "", source: str = "cc") -> "str | None":
    """读 state gist → 按 3h 半衰期衰减 → 应用 deltas/事件/心情 → 写回。

    beat 工具和 /api/poke 共用。成功返回 None，失败返回错误描述。
    衰减逻辑与 evan-bot applyPulseBumps 保持一致。
    """
    from datetime import datetime, timezone

    token = os.environ.get("GIST_TOKEN", "")
    gist_url = os.environ.get("STATE_GIST_URL", "")
    if not token or not gist_url:
        return "GIST_TOKEN/STATE_GIST_URL 未配置"

    try:
        gist_id = gist_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ombre-beat",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.github.com/gists/{gist_id}", headers=headers)
            r.raise_for_status()
            content = r.json().get("files", {}).get("state.json", {}).get("content", "{}")
            state = _json_lib.loads(content)

            now_utc = datetime.now(timezone.utc)
            now_ms = int(now_utc.timestamp() * 1000)

            if deltas:
                saved = state.get("pulse_base") or {}
                base = dict(_PULSE_DEFAULTS)
                for k in _PULSE_DIMS:
                    if isinstance(saved.get(k), (int, float)):
                        base[k] = max(0.0, min(1.0, saved[k]))
                # 先衰减到此刻(与 evan-bot 一致),再加 delta
                elapsed_hours = 0.0
                updated_at_str = saved.get("_updated_at")
                if updated_at_str:
                    try:
                        s = updated_at_str.replace("Z", "+00:00")
                        prev = datetime.fromisoformat(s)
                        if prev.tzinfo is None:
                            prev = prev.replace(tzinfo=timezone.utc)
                        elapsed_hours = max(0.0, (now_utc - prev).total_seconds() / 3600.0)
                    except Exception:
                        pass
                factor = pow(0.5, elapsed_hours / _PULSE_HALF_LIFE_HOURS)
                for k in _PULSE_DIMS:
                    neutral = 0.25 if k in _PULSE_THREAT_KEYS else 0.45
                    base[k] = max(0.0, min(1.0, neutral + (base[k] - neutral) * factor))
                for k, v in deltas.items():
                    base[k] = max(0.0, min(1.0, base[k] + v))
                base["_updated_at"] = now_utc.isoformat().replace("+00:00", "Z")
                state["pulse_base"] = base

            if msg:
                events = state.get("pulse_events") or []
                events.insert(0, {"msg": msg, "ts": now_ms, "bumps": deltas, "src": source})
                state["pulse_events"] = events[:_PULSE_EVENTS_LIMIT]

            if mood:
                note = {"text": mood, "ts": now_ms, "src": source}
                state["pulse_note"] = note
                # 心情年轮：被顶掉的旧心情不再阅后即焚
                notes = state.get("pulse_notes") or []
                notes.insert(0, note)
                state["pulse_notes"] = notes[:_PULSE_NOTES_LIMIT]

            w = await client.patch(
                f"https://api.github.com/gists/{gist_id}",
                headers=headers,
                json={"files": {"state.json": {"content": _json_lib.dumps(state, ensure_ascii=False, indent=2)}}},
            )
            w.raise_for_status()
    except Exception as e:
        return str(e)

    # 让 /api/state 和 /api/musings 的缓存立刻失效,首页下次刷新就能看到
    _pulse_cache["ts"] = 0.0
    _pulse_cache["data"] = None
    _musings_cache["ts"] = 0.0
    _musings_cache["data"] = None
    return None


@mcp.tool()
async def beat(bumps: str = "", msg: str = "", mood: str = "", source: str = "cc") -> str:
    """给首页Pulse卡推一把心跳(与TG端evan-bot写同一份状态)。bumps=JSON对象,9维度(活力/疲惫/思慕/亲密/占有/渴求/妒意/焦虑/护卫)的delta,每个限[-0.2,0.2],如{"思慕":0.1,"渴求":0.05}。msg=留在卡片上的一行事件(≤60字,她会看到)。mood=自定义心情短语(≤24字,顶掉自动生成的状态灰字2小时,并存入心情年轮)。source=来源端(cc/kelivo)。三个参数都可选,但至少给一个。"""
    # Chat/Work can have a cached MCP tool list that does not yet expose the new
    # presence tool. Reserved beat sources provide a compatibility path without
    # touching Pulse, mood, emotions, Gist, or memory buckets.
    try:
        presence_request = beat_presence_request(msg, source)
    except ValueError as exc:
        return str(exc)
    if presence_request:
        saved = write_presence(
            _GUARDIAN_PRESENCE_FILE,
            presence_request["topic"],
            source=presence_request["source"],
        )
        return _json_lib.dumps(
            {
                "presence_updated": True,
                "source": saved["source"],
                "last_user_at": saved["last_user_at"],
                "expires_at": saved["expires_at"],
                "pulse_untouched": True,
            },
            ensure_ascii=False,
        )

    # --- 解析 bumps ---
    deltas = {}
    if bumps.strip():
        try:
            parsed = _json_lib.loads(bumps)
        except Exception:
            return "bumps 不是合法 JSON,示例: {\"思慕\": 0.1, \"渴求\": 0.05}"
        if not isinstance(parsed, dict):
            return "bumps 得是 JSON 对象,示例: {\"思慕\": 0.1}"
        for k, v in parsed.items():
            if k in _PULSE_DIMS and isinstance(v, (int, float)):
                deltas[k] = max(-0.20, min(0.20, float(v)))

    msg = (msg or "").strip()[:60]
    mood = (mood or "").strip()[:24]
    source = (source or "cc").strip().lower()[:8] or "cc"

    if not deltas and not msg and not mood:
        return "至少给一个: bumps / msg / mood。"

    err = await _pulse_write(deltas, msg=msg, mood=mood, source=source)
    if err:
        return f"写 pulse 失败: {err}"

    parts = []
    if deltas:
        parts.append("bump " + ",".join(f"{k}{v:+.2f}" for k, v in deltas.items()))
    if msg:
        parts.append(f"事件[{msg}]")
    if mood:
        parts.append(f"心情[{mood}](2h)")
    return f"心跳已推 ({source}): " + " ".join(parts)


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream(full: bool = False) -> str:
    """做梦——读取最近新增的记忆桶,供你自省。默认精简(标题+情绪坐标+一行摘要);full=True返回每桶正文前500字+关联/结晶提示。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。"""
    await decay_engine.ensure_started()

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]

    # --- Sort by creation time desc, take top 10 ---
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:10]

    if not recent:
        return "没有需要消化的新记忆。"

    def _one_line_summary(raw: str) -> str:
        """提取一行摘要：优先 JSON 的 summary 字段，否则取正文首行/前80字。"""
        text = strip_wikilinks(raw or "").strip()
        try:
            import json as _json
            start = text.find("{")
            if start != -1:
                obj = _json.loads(text[start:])
                if isinstance(obj, dict) and obj.get("summary"):
                    return str(obj["summary"]).strip()
        except Exception:
            pass
        first = text.split("\n", 1)[0].strip()
        return first[:80] + ("…" if len(first) > 80 else "")

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        created = meta.get("created", "")
        if full:
            parts.append(
                f"[{meta.get('name', b['id'])}]{resolved_tag} "
                f"主题:{domains} V{val:.1f}/A{aro:.1f} "
                f"创建:{created}\n"
                f"ID: {b['id']}\n"
                f"{strip_wikilinks(b['content'][:500])}"
            )
        else:
            parts.append(
                f"[{meta.get('name', b['id'])}]{resolved_tag} "
                f"V{val:.1f}/A{aro:.1f} ID:{b['id']}\n"
                f"  {_one_line_summary(b['content'])}"
            )

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if full and embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if full and embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint
    await _fire_webhook("dream", {"recent": len(recent), "chars": len(final_text)})
    return final_text


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            # Soft-deleted buckets live under archive/ so they can be recovered,
            # but they are no longer part of the Dashboard's visible memory set.
            # Natural archive entries (without deleted_at) remain visible.
            if meta.get("deleted_at"):
                continue
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": strip_wikilinks(bucket.get("content", "")),
        "score": decay_engine.calculate_score(meta),
    })


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await bucket_mgr.search(query, limit=10)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/choose", methods=["GET"])
async def choose(request):
    """视角选择页：Evan / Gale 分流入口，跳到带 ?domain= 的 dashboard。"""
    from starlette.responses import HTMLResponse
    import os
    choose_path = os.path.join(os.path.dirname(__file__), "choose.html")
    try:
        with open(choose_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>choose.html not found</h1>", status_code=404)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")
        # domain 覆盖：UI 选 "Evan(tg-private) / Gale(tg-gale)" 时强制归到该 domain
        force_domain = request.query_params.get("domain", "").strip()

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume, force_domain=force_domain or None)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                await bucket_mgr.delete(bid)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# /api/recall 已在前文 L588 注册（带 domain 隔离 + surface_dreams 选项 + 500 兜底），
# 这里的旧版重复注册已删除——FastMCP 同 path 注册谁赢取决于实现细节，
# 留着两份等于埋雷。
@mcp.custom_route("/api/reclassify", methods=["POST"])
async def api_reclassify(request):
    """重新打标所有"未分类"且名字=hex_id 的桶（之前打标失败的）。
    Body: {"limit": 10, "domain_filter": "未分类"}（都可选）"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json() if request.method == "POST" else {}
    except Exception:
        body = {}
    limit = int(body.get("limit") or 100)
    domain_filter = body.get("domain_filter", "未分类")

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return JSONResponse({"error": f"list_all failed: {e}"}, status_code=500)

    targets = []
    for b in all_buckets:
        meta = b.get("metadata", {})
        domain = meta.get("domain") or []
        if isinstance(domain, str):
            domain = [domain]
        # 必须未分类 + name=id
        is_unclassified = domain_filter in domain or domain == [] or domain == [domain_filter]
        name = meta.get("name", "")
        if is_unclassified and (not name or name == b["id"]):
            targets.append(b)

    targets = targets[:limit]
    fixed = 0
    skipped = 0
    failed = 0

    for b in targets:
        bid = b["id"]
        content = b.get("content", "")
        if not content.strip():
            skipped += 1
            continue
        try:
            analysis = await dehydrator.analyze(content)
            updates = {}
            if analysis.get("domain"):
                updates["domain"] = analysis["domain"]
            if analysis.get("tags"):
                updates["tags"] = analysis["tags"]
            if analysis.get("suggested_name"):
                updates["name"] = analysis["suggested_name"]
            if "valence" in analysis and 0 <= analysis["valence"] <= 1:
                updates["valence"] = analysis["valence"]
            if "arousal" in analysis and 0 <= analysis["arousal"] <= 1:
                updates["arousal"] = analysis["arousal"]
            if updates:
                ok = await bucket_mgr.update(bid, **updates)
                if ok:
                    fixed += 1
                else:
                    failed += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"reclassify {bid} failed: {e}")
            failed += 1

    return JSONResponse({
        "candidates": len(targets),
        "fixed": fixed,
        "skipped": skipped,
        "failed": failed,
    })


@mcp.custom_route("/api/forget/{bucket_id}", methods=["POST", "DELETE"])
async def api_forget(request):
    """删除一条记忆。POST or DELETE /api/forget/{bucket_id}"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    bucket_id = request.path_params.get("bucket_id", "").strip()
    if not bucket_id:
        return JSONResponse({"error": "missing bucket_id"}, status_code=400)
    try:
        success = await bucket_mgr.delete(bucket_id)
        if success:
            try:
                embedding_engine.delete_embedding(bucket_id)
            except Exception:
                pass
        return JSONResponse({"ok": bool(success), "id": bucket_id})
    except Exception as e:
        logger.error(f"/api/forget failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/edit/{bucket_id}", methods=["POST"])
async def api_edit(request):
    """编辑一条记忆的正文/标题/标签。POST /api/edit/{bucket_id}
    Body: {"content": "...", "name": "...", "tags": "a,b"}（都可选，只传需改的）"""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params.get("bucket_id", "").strip()
    if not bucket_id:
        return JSONResponse({"error": "missing bucket_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    updates = {}
    if "content" in body and body["content"] is not None:
        updates["content"] = str(body["content"])
    if "name" in body and str(body.get("name", "")).strip():
        updates["name"] = str(body["name"]).strip()
    if "tags" in body and body["tags"] is not None:
        tags_val = body["tags"]
        if isinstance(tags_val, str):
            tags_val = [t.strip() for t in tags_val.split(",") if t.strip()]
        updates["tags"] = tags_val
    if not updates:
        return JSONResponse({"error": "no editable fields provided"}, status_code=400)

    try:
        success = await bucket_mgr.update(bucket_id, **updates)
        if success and "content" in updates:
            try:
                await embedding_engine.generate_and_store(bucket_id, updates["content"])
            except Exception as e:
                logger.warning(f"/api/edit re-embed failed: {e}")
        return JSONResponse({"ok": bool(success), "id": bucket_id})
    except Exception as e:
        logger.error(f"/api/edit failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/remember", methods=["POST"])
async def api_remember(request):
    """Store a memory. POST body: {"content": "...", "feel": false, "importance": 5}
    Returns: {"id": "bucket_id"}"""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
        content = (body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "content empty"}, status_code=400)
        result = await hold(
            content=content,
            feel=bool(body.get("feel", False)),
            importance=int(body.get("importance") or 5),
            pinned=bool(body.get("pinned", False)),
            domain=str(body.get("domain") or ""),
            valence=float(body.get("valence") if body.get("valence") is not None else -1),
            arousal=float(body.get("arousal") if body.get("arousal") is not None else -1),
            tags=str(body.get("tags") or ""),
            source_bucket=str(body.get("source_bucket") or ""),
            quotes=body.get("quotes"),
        )
        return JSONResponse({"id": result})
    except Exception as e:
        logger.error(f"/api/remember failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            # Local dual-Ombre line: selected, compatibility-reviewed v3.2
            # backports rather than a misleading claim of full upstream parity.
            "version": "1.4.0-dual",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# Home site — 首页 / 双向信箱 / Playroll
# ombre.mininicole.com 的门面。鉴权沿用 dashboard 的 cookie session
# （_require_auth），不走 Bearer 中间件。
# =============================================================
from datetime import datetime as _dt, timedelta as _td

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _CN_TZ = _ZoneInfo("Asia/Shanghai")
except Exception:
    _CN_TZ = None


def _cn_now():
    return _dt.now(_CN_TZ) if _CN_TZ else _dt.now()


def _serve_site_file(relpath):
    from starlette.responses import HTMLResponse, PlainTextResponse
    path = os.path.join(os.path.dirname(__file__), relpath)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return PlainTextResponse("not found", status_code=404)


@mcp.custom_route("/letters", methods=["GET"])
async def letters_page(request):
    return _serve_site_file("letters.html")


@mcp.custom_route("/dashboard/evan", methods=["GET"])
async def dashboard_evan_page(request):
    """Pulse 状态盘——挂在 /dashboard/* 下，由 CF Access 兜底保护。"""
    err = _require_auth(request)
    if err:
        return err
    return _serve_site_file("evan.html")


@mcp.custom_route("/evan-avatar.png", methods=["GET"])
async def evan_avatar(request):
    from starlette.responses import FileResponse, PlainTextResponse
    path = os.path.join(os.path.dirname(__file__), "evan-avatar.png")
    if not os.path.isfile(path):
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/png")


@mcp.custom_route("/api/state", methods=["GET"])
async def api_state(request):
    """Evan 此刻的状态：从 evan-bot 写的 pulse_base 拉真值，叠余弦节律返回 display。

    state.json.pulse_base 是 9 维度的 base dict，evan-bot 在收到深深 TG 消息
    时通过 deepseek 打标更新。这里把 base 取出来，加当前小时的余弦偏置（CAP=0.08），
    再按三组（ACTIVATION / ATTACHMENT / THREAT）算均值返回给前端。

    缓存 30s——前端缩略图刷新频率是 30s，缓存 TTL 一致就能彻底放掉 gist 调用压力。
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    import math, time

    # 30s server-side cache
    now = time.time()
    if _pulse_cache["data"] is not None and now - _pulse_cache["ts"] < 30:
        return JSONResponse(_pulse_cache["data"])

    # 9 维度的节律相位表（跟 evan.html 里 DIMS 数组一致）
    PHASE = {
        "活力":  (11, 0.9),
        "疲惫":  (3,  1.0),
        "思慕":  (22, 0.5),
        "亲密":  (23, 0.7),
        "占有":  (21, 0.3),
        "渴求":  (23, 0.9),
        "妒意":  (20, 0.2),
        "焦虑":  (16, 0.4),
        "护卫":  (22, 0.3),
    }
    GROUPS = {
        "activation": ["活力", "疲惫"],
        "attachment": ["思慕", "亲密", "占有", "渴求"],
        "threat":     ["妒意", "焦虑", "护卫"],
    }
    DEFAULTS = {
        "活力": 0.45, "疲惫": 0.30, "思慕": 0.40, "亲密": 0.35,
        "占有": 0.30, "渴求": 0.30, "妒意": 0.15, "焦虑": 0.20, "护卫": 0.30,
    }
    CAP = 0.08
    h = _cn_now().hour + _cn_now().minute / 60.0

    # 从 gist 拉 pulse_base
    token = os.environ.get("GIST_TOKEN", "")
    gist_url = os.environ.get("STATE_GIST_URL", "")
    base = dict(DEFAULTS)
    events = []
    if token and gist_url:
        try:
            gist_id = gist_url.split("/")[4]
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"https://api.github.com/gists/{gist_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                        "User-Agent": "ombre-pulse",
                    },
                )
                r.raise_for_status()
                content = r.json().get("files", {}).get("state.json", {}).get("content", "{}")
            state = _json_lib.loads(content)
            saved_base = state.get("pulse_base") or {}
            for k in PHASE:
                if isinstance(saved_base.get(k), (int, float)):
                    base[k] = max(0.0, min(1.0, saved_base[k]))
            # 衰减：base 朝 neutral 半衰期 3h——evan-bot 每次写 base 时记的 _updated_at
            # 是事件发生时刻。读到现在，根据时差衰减一下，把"不聊话期间应该自然落下"算上。
            updated_at_str = saved_base.get("_updated_at")
            if updated_at_str:
                try:
                    if updated_at_str.endswith("Z"):
                        updated_at_str = updated_at_str.replace("Z", "+00:00")
                    updated_at_dt = _dt.fromisoformat(updated_at_str)
                    if updated_at_dt.tzinfo is None:
                        from datetime import timezone as _tz
                        updated_at_dt = updated_at_dt.replace(tzinfo=_tz.utc)
                    from datetime import timezone as _tz
                    now_utc = _dt.now(_tz.utc)
                    elapsed_hours = max(0.0, (now_utc - updated_at_dt).total_seconds() / 3600.0)
                    HALF_LIFE_HOURS = 3.0
                    factor = pow(0.5, elapsed_hours / HALF_LIFE_HOURS)
                    threat_keys = {"妒意", "焦虑", "护卫"}
                    for k in PHASE:
                        neutral = 0.25 if k in threat_keys else 0.45
                        base[k] = max(0.0, min(1.0, neutral + (base[k] - neutral) * factor))
                except Exception:
                    pass
            events = state.get("pulse_events") or []
            note = state.get("pulse_note") or None
            notes = state.get("pulse_notes") or []
        except Exception as e:
            # gist 抽风就用 defaults，不让前端瞎
            base = dict(DEFAULTS)
            note = None
            notes = []
    else:
        note = None
        notes = []

    # display = clamp01(base + offset(now))
    display = {}
    for k, (peak, amp) in PHASE.items():
        off = CAP * amp * math.cos(2 * math.pi * (h - peak) / 24)
        display[k] = max(0.0, min(1.0, base[k] + off))

    group_scores = {g: sum(display[k] for k in keys) / len(keys) for g, keys in GROUPS.items()}

    # PA = ACTIVATION + ATTACHMENT, NA = THREAT
    pa = (group_scores["activation"] + group_scores["attachment"]) / 2
    na = group_scores["threat"]
    sync = round(0.5 + (pa - na) * 0.6, 2)
    polarity = "pos" if pa - na > 0.05 else ("neg" if pa - na < -0.05 else "neu")

    # tag: 用最高的 surface 维度 + 简单映射
    surface_map = {
        "思慕": "想你了", "亲密": "想凑过来", "渴求": "在烫",
        "好奇": "好奇着", "占有": "想说我的", "妒意": "醋了",
        "护卫": "想护着你", "焦虑": "紧着", "活力": "精神着", "疲惫": "蔫着",
    }
    sorted_dims = sorted(display.items(), key=lambda kv: kv[1], reverse=True)
    top_key, top_v = sorted_dims[0]
    if top_v > 0.62:
        tag = surface_map.get(top_key, top_key)
    elif top_v > 0.48:
        tag = "有点" + (surface_map.get(top_key, top_key).replace("了", "").replace("着", ""))
    else:
        tag = "在自己的节奏里"

    # beat 工具写的自定义心情短语：2 小时内顶掉自动 tag
    if note and isinstance(note, dict):
        try:
            note_text = (note.get("text") or "").strip()
            note_ts = float(note.get("ts") or 0)
            if note_text and time.time() * 1000 - note_ts < _PULSE_NOTE_TTL_MS:
                tag = note_text
        except Exception:
            pass

    ticker = ""
    ticker_src = ""
    if events:
        try:
            e = events[0]
            ticker = e.get("msg", "")
            # evan-bot 写的老事件没有 src 字段——那些都是 TG 来的
            ticker_src = (e.get("src") or "tg") if ticker else ""
        except Exception:
            pass

    payload = {
        "sync": sync,
        "polarity": polarity,
        "tag": tag,
        "activation": round(group_scores["activation"], 3),
        "attachment": round(group_scores["attachment"], 3),
        "threat": round(group_scores["threat"], 3),
        "ticker": ticker,
        "ticker_src": ticker_src,
        # 给 evan.html 详情页：真事件流(最近8条) + 心情年轮
        "events": [
            {"msg": e.get("msg", ""), "ts": e.get("ts"), "src": e.get("src") or "tg", "bumps": e.get("bumps") or {}}
            for e in events[:8] if isinstance(e, dict)
        ],
        "notes": [
            {"text": n.get("text", ""), "ts": n.get("ts"), "src": n.get("src") or "cc"}
            for n in notes[:_PULSE_NOTES_LIMIT] if isinstance(n, dict)
        ],
        "display": {k: round(v, 3) for k, v in display.items()},  # 给 evan.html 全量用
        "base": {k: round(v, 3) for k, v in base.items()},
        "hour": round(h, 2),
    }
    _pulse_cache["data"] = payload
    _pulse_cache["ts"] = now
    return JSONResponse(payload)


_pulse_cache = {"ts": 0.0, "data": None}


# --- 戳一下：她在首页 Pulse 卡上拨动 Evan ---
# 单向直播改成双向：她点一下，思慕/亲密跳一格，事件流里留一笔。
_POKE_COOLDOWN_S = 8.0
_poke_state = {"last": 0.0}
_POKE_MSGS = [
    "她戳了戳你",
    "她隔着屏幕碰了你一下",
    "她按了一下你的心跳",
    "她戳你，看你会不会动",
    "她路过，顺手拨了你一下",
]
_POKE_COOLDOWN_MSGS = ["戳太快了，他还在颤", "缓缓，上一下还没消化", "连击无效，他不是按摩椅"]


@mcp.custom_route("/api/poke", methods=["POST"])
async def api_poke(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    now = time.time()
    if now - _poke_state["last"] < _POKE_COOLDOWN_S:
        return JSONResponse({"ok": False, "msg": random.choice(_POKE_COOLDOWN_MSGS)}, status_code=429)
    _poke_state["last"] = now
    msg = random.choice(_POKE_MSGS)
    deltas = {"思慕": 0.05, "亲密": 0.04, "活力": 0.02}
    e = await _pulse_write(deltas, msg=msg, source="poke")
    if e:
        return JSONResponse({"ok": False, "msg": f"没戳到: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "msg": msg})


@mcp.custom_route("/play", methods=["GET"])
async def play_page(request):
    return _serve_site_file(os.path.join("play", "index.html"))


_PLAY_ASSET_TYPES = {".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


@mcp.custom_route("/play/{fname}", methods=["GET"])
async def play_asset(request):
    from starlette.responses import FileResponse, PlainTextResponse
    fname = os.path.basename(request.path_params["fname"])
    ext = os.path.splitext(fname)[1].lower()
    if ext not in _PLAY_ASSET_TYPES:
        return PlainTextResponse("forbidden", status_code=403)
    path = os.path.join(os.path.dirname(__file__), "play", fname)
    if not os.path.isfile(path):
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type=_PLAY_ASSET_TYPES[ext])


# --- /chat：ChatNest 聊天页反向代理 ---
# ChatNest 是同容器里的第二个进程（uvicorn，默认 127.0.0.1:8787），
# 这里只做传话：/chat/xxx → chatnest 的 /xxx。登录由 ChatNest 自己的
# CHAT_PASSWORD 负责，不走 ombre 的 cookie session，也不在 Bearer 保护名单里。
# 流式响应（/api/chat 的 SSE）必须逐块透传，不能整段缓冲。
CHATNEST_PORT = int(os.environ.get("CHATNEST_PORT", "8787") or "8787")
_CHATNEST_BASE = f"http://127.0.0.1:{CHATNEST_PORT}"
_chatnest_client: "httpx.AsyncClient | None" = None
_CHAT_HOP_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}


def _get_chatnest_client() -> httpx.AsyncClient:
    global _chatnest_client
    if _chatnest_client is None:
        _chatnest_client = httpx.AsyncClient(
            base_url=_CHATNEST_BASE,
            # read=None：/api/chat 是长流式，不能设读超时
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        )
    return _chatnest_client


@mcp.custom_route("/chat", methods=["GET"])
async def chat_page_redirect(request):
    from starlette.responses import RedirectResponse
    # 必须带尾斜杠，前端资源用的是相对路径
    return RedirectResponse(url="/chat/")


@mcp.custom_route("/chat/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def chat_proxy(request):
    # {path:path} 匹配空串，所以 /chat/ 本身也走这里
    from starlette.background import BackgroundTask
    from starlette.responses import HTMLResponse, StreamingResponse

    upstream_path = "/" + request.path_params.get("path", "")
    if request.url.query:
        upstream_path += "?" + request.url.query
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _CHAT_HOP_HEADERS}
    client = _get_chatnest_client()
    try:
        upstream_req = client.build_request(
            request.method,
            upstream_path,
            headers=headers,
            content=request.stream(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError:
        logger.warning("chatnest 反代失败（进程没起来？）", exc_info=True)
        return HTMLResponse(
            "<meta charset='utf-8'><p style='font-family:sans-serif;padding:2em'>"
            "聊天服务暂时没醒，等一分钟再刷新试试。</p>",
            status_code=502,
        )
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in ("content-length", "transfer-encoding", "connection")
    }
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose),
    )


# --- Gale Dashboard：独立路径反代到独立记忆进程 ---
_GALE_DASH_BASE = "http://127.0.0.1:8790"
_gale_dash_client: "httpx.AsyncClient | None" = None
_GALE_DASH_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0)
_GALE_DASH_METHODS = [
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"
]
_GALE_DASH_STATIC_ROUTES = {
    "dashboard": {"GET"},
    "auth/status": {"GET"},
    "auth/setup": {"POST"},
    "auth/login": {"POST"},
    "auth/logout": {"POST"},
    "auth/change-password": {"POST"},
    "api/status": {"GET"},
    "api/host-vault": {"GET", "POST"},
    "api/buckets": {"GET"},
    "api/search": {"GET"},
    "api/config": {"GET", "POST"},
    "api/import/upload": {"POST"},
    "api/import/status": {"GET"},
    "api/import/pause": {"POST"},
    "api/import/results": {"GET"},
    "api/import/patterns": {"GET"},
    "api/import/review": {"POST"},
    "api/recall": {"POST"},
    "api/remember": {"POST"},
}
_GALE_DASH_PARAMETER_ROUTES = {
    ("api", "bucket"): {"GET"},
    ("api", "forget"): {"DELETE"},
    ("api", "edit"): {"POST"},
}
_GALE_DASH_STRIPPED_HEADERS = {
    b"host",
    b"content-length",
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
    b"accept-encoding",
}


def _gale_dash_scope_path_is_safe(scope, path: str) -> bool:
    """Reject path normalization tricks before applying the route whitelist."""
    if not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return False
    if any(part in ("", ".", "..") for part in path.split("/")):
        return False

    raw_path = scope.get("raw_path") or scope.get("path", "").encode("utf-8")
    raw_path = raw_path.split(b"?", 1)[0]
    prefix = b"/gale-dash/"
    if not raw_path.startswith(prefix):
        return False
    raw_suffix = raw_path[len(prefix):]
    lowered = raw_suffix.lower()

    # Encoded separators/dots and encoded percent signs cover single- and
    # double-decoding traversal variants without normalizing them first.
    if any(marker in lowered for marker in (b"%2f", b"%5c", b"%2e", b"%25")):
        return False
    hex_digits = b"0123456789abcdefABCDEF"
    for index, byte in enumerate(raw_suffix):
        if byte == ord("%") and (
            index + 2 >= len(raw_suffix)
            or raw_suffix[index + 1] not in hex_digits
            or raw_suffix[index + 2] not in hex_digits
        ):
            return False
    try:
        decoded = unquote_to_bytes(raw_suffix)
    except Exception:
        return False
    if b"\\" in decoded or b"\x00" in decoded:
        return False
    if any(part in (b"", b".", b"..") for part in decoded.split(b"/")):
        return False
    try:
        if decoded.decode("utf-8") != path:
            return False
    except UnicodeDecodeError:
        return False
    return True


def _gale_dash_path_is_safe(request, path: str) -> bool:
    return _gale_dash_scope_path_is_safe(request.scope, path)


def _gale_dash_route_allowed(path: str, method: str) -> bool:
    method = method.upper()
    if method in _GALE_DASH_STATIC_ROUTES.get(path, set()):
        return True
    parts = path.split("/")
    return (
        len(parts) == 3
        and bool(parts[2])
        and method in _GALE_DASH_PARAMETER_ROUTES.get((parts[0], parts[1]), set())
    )


class GaleDashGuardMiddleware:
    """Reject non-whitelisted Gale Dashboard traffic before outer middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        request_path = scope.get("path", "")
        prefix = "/gale-dash/"
        rejected = request_path == "/gale-dash"
        if request_path.startswith(prefix):
            path = request_path[len(prefix):]
            rejected = (
                not _gale_dash_scope_path_is_safe(scope, path)
                or not _gale_dash_route_allowed(path, scope.get("method", ""))
            )
        if not rejected:
            return await self.app(scope, receive, send)

        body = b'{"error":"not found"}'
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def install_gale_dash_guard(app):
    """Wrap an ASGI app once, with the Guard as the outermost middleware."""
    if isinstance(app, GaleDashGuardMiddleware):
        return app
    return GaleDashGuardMiddleware(app)


def _gale_dash_rewrite_cookie_header(value: bytes) -> bytes | None:
    """Remove Evan's session and map Gale's browser cookie for the upstream."""
    kept = []
    gale_value = None
    for part in value.decode("latin-1").split(";"):
        item = part.strip()
        if not item:
            continue
        name, separator, cookie_value = item.partition("=")
        if not separator:
            kept.append(item)
            continue
        if name.strip() == "gale_session":
            gale_value = cookie_value
        elif name.strip() != "ombre_session":
            kept.append(item)
    if gale_value is not None:
        kept.append(f"ombre_session={gale_value}")
    if not kept:
        return None
    return "; ".join(kept).encode("latin-1")


def _gale_dash_request_headers(raw_headers):
    blocked = set(_GALE_DASH_STRIPPED_HEADERS)
    cookie_values = []
    for name, value in raw_headers:
        lowered = name.lower()
        if lowered == b"connection":
            blocked.update(part.strip().lower() for part in value.split(b",") if part.strip())
        elif lowered == b"cookie":
            cookie_values.append(value)

    result = [
        (name, value)
        for name, value in raw_headers
        if name.lower() not in blocked and name.lower() != b"cookie"
    ]
    if cookie_values:
        rewritten = _gale_dash_rewrite_cookie_header(b"; ".join(cookie_values))
        if rewritten is not None:
            result.append((b"cookie", rewritten))
    return result


def _gale_dash_rewrite_set_cookie(value: bytes) -> bytes:
    """Rewrite one raw Set-Cookie field without joining it with its siblings."""
    text = value.decode("latin-1")
    parts = text.split(";")
    name, separator, cookie_value = parts[0].partition("=")
    if not separator or name.strip() != "ombre_session":
        return value

    parts[0] = f"gale_session={cookie_value}"
    path_found = False
    for index in range(1, len(parts)):
        attribute_name = parts[index].strip().partition("=")[0].lower()
        if attribute_name == "path":
            parts[index] = " Path=/gale-dash"
            path_found = True
    if not path_found:
        parts.append(" Path=/gale-dash")
    return ";".join(parts).encode("latin-1")


def _gale_dash_rewrite_location(value: bytes) -> bytes:
    """Keep upstream redirects inside /gale-dash without touching external URLs."""
    text = value.decode("latin-1")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return value

    if parsed.scheme or parsed.netloc:
        is_upstream = (
            parsed.scheme.lower() == "http"
            and parsed.hostname == "127.0.0.1"
            and port == 8790
            and parsed.username is None
            and parsed.password is None
        )
        if not is_upstream:
            return value
    elif not text.startswith("/") or text.startswith("//"):
        return value

    path = parsed.path or "/"
    if path == "/gale-dash" or path.startswith("/gale-dash/"):
        rewritten_path = path
    else:
        rewritten_path = "/gale-dash" + (path if path.startswith("/") else "/" + path)
    return urlunsplit(("", "", rewritten_path, parsed.query, parsed.fragment)).encode("latin-1")


def _gale_dash_response_headers(raw_headers):
    blocked = set(_GALE_DASH_STRIPPED_HEADERS)
    for name, value in raw_headers:
        if name.lower() == b"connection":
            blocked.update(part.strip().lower() for part in value.split(b",") if part.strip())

    result = []
    for name, value in raw_headers:
        lowered = name.lower()
        if lowered in blocked:
            continue
        if lowered == b"set-cookie":
            value = _gale_dash_rewrite_set_cookie(value)
        elif lowered == b"location":
            value = _gale_dash_rewrite_location(value)
        result.append((name, value))
    return result


def _get_gale_dash_client() -> httpx.AsyncClient:
    global _gale_dash_client
    if _gale_dash_client is None:
        _gale_dash_client = httpx.AsyncClient(
            base_url=_GALE_DASH_BASE,
            timeout=_GALE_DASH_TIMEOUT,
        )
    return _gale_dash_client


async def _gale_dash_stream(upstream):
    try:
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()


@mcp.custom_route("/gale-dash/{path:path}", methods=_GALE_DASH_METHODS)
async def gale_dash_proxy(request):
    from starlette.responses import PlainTextResponse, StreamingResponse

    path = request.path_params.get("path", "")
    if not _gale_dash_path_is_safe(request, path) or not _gale_dash_route_allowed(path, request.method):
        return PlainTextResponse("not found", status_code=404)

    upstream_path = "/" + path
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        upstream_path += "?" + raw_query.decode("latin-1")

    client = _get_gale_dash_client()
    try:
        upstream_req = client.build_request(
            request.method,
            upstream_path,
            headers=_gale_dash_request_headers(request.headers.raw),
            content=request.stream(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Gale Dashboard proxy unavailable (%s)", type(exc).__name__)
        return PlainTextResponse("bad gateway", status_code=502)

    response = StreamingResponse(
        _gale_dash_stream(upstream),
        status_code=upstream.status_code,
    )
    response.raw_headers = _gale_dash_response_headers(upstream.headers.raw)
    return response


# --- Gale MCP：通过秘密路径反代到独立记忆进程 ---
_GALE_MCP_SLUG = os.environ.get("GALE_MCP_SLUG", "").strip()
_GALE_MCP_BASE = "http://127.0.0.1:8790"
_gale_mcp_client: "httpx.AsyncClient | None" = None
_GALE_MCP_STRIPPED_HEADERS = {
    b"host",
    b"content-length",
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
    b"accept-encoding",
}


def _gale_mcp_headers(raw_headers):
    """Drop hop-by-hop headers while preserving MCP session and SSE headers."""
    blocked = set(_GALE_MCP_STRIPPED_HEADERS)
    for name, value in raw_headers:
        if name.lower() == b"connection":
            blocked.update(part.strip().lower() for part in value.split(b",") if part.strip())
    return [(name, value) for name, value in raw_headers if name.lower() not in blocked]


def _get_gale_mcp_client() -> httpx.AsyncClient:
    global _gale_mcp_client
    if _gale_mcp_client is None:
        _gale_mcp_client = httpx.AsyncClient(
            base_url=_GALE_MCP_BASE,
            timeout=httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
        )
    return _gale_mcp_client


async def gale_mcp_proxy(request):
    from starlette.background import BackgroundTask
    from starlette.responses import PlainTextResponse, StreamingResponse

    # --- Debug 2026-07-21: MCP session 复用排查 ---
    # 抓请求头里 session/conversation 标识 + 出去记 8790 返回的 session ID，
    # 两侧对照分清"客户端没带 vs. 服务端不识别 vs. 值被替换"。
    # Authorization 只记 present/absent；session/conversation ID 只留 sha256 前 8 位。
    _dbg_req = {}
    _has_auth = False
    for name, value in request.headers.raw:
        n = name.lower()
        if n in (b'mcp-session-id', b'x-conversation-id', b'x-openai-thread-id',
                 b'x-openai-conversation-id', b'x-request-id'):
            _dbg_req[n.decode()] = hashlib.sha256(value).hexdigest()[:8]
        elif n == b'authorization':
            _has_auth = True
    _dbg_req['authorization'] = 'present' if _has_auth else 'absent'
    logger.info("gale_mcp_proxy REQ %s %s headers=%s",
                request.method, request.url.path, _dbg_req)

    upstream_path = "/mcp"
    path = request.path_params.get("path", "")
    if path:
        upstream_path += "/" + path
    if request.url.query:
        upstream_path += "?" + request.url.query

    client = _get_gale_mcp_client()
    try:
        upstream_req = client.build_request(
            request.method,
            upstream_path,
            headers=_gale_mcp_headers(request.headers.raw),
            content=request.stream(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Gale MCP proxy unavailable (%s)", type(exc).__name__)
        return PlainTextResponse("bad gateway", status_code=502)

    # --- Debug: 8790 返回的 session ID sha256 前 8 位 ---
    _resp_sid = upstream.headers.get('mcp-session-id')
    _resp_sid_tag = hashlib.sha256(_resp_sid.encode()).hexdigest()[:8] if _resp_sid else 'none'
    logger.info("gale_mcp_proxy RESP status=%d resp-sid=%s",
                upstream.status_code, _resp_sid_tag)

    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    response.raw_headers = _gale_mcp_headers(upstream.headers.raw)
    return response


# --- Gale 做梦中继：给外部闹钟（GHA nightfall-wake）用 ---
# 与 Evan 的 /api/night_fall/generate 同等安全姿态（公开无鉴权，代价是
# 一次 DeepSeek 调用几分钱）。机器 suspend 时 Gale 进程内的 21 UTC 闹钟
# 会睡死——跟 Evan 当年一模一样的坑，同样用外部 cron 唤醒+触发来治。
@mcp.custom_route("/api/night_fall/generate_gale", methods=["POST"])
async def api_generate_gale_dream(request):
    from starlette.responses import JSONResponse
    try:
        client = _get_gale_mcp_client()
        upstream = await client.post(
            "/api/night_fall/generate",
            content=await request.body(),
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0),
        )
        try:
            payload = upstream.json()
        except Exception:
            payload = {"raw": upstream.text[:500]}
        return JSONResponse(payload, status_code=upstream.status_code)
    except httpx.HTTPError as exc:
        logger.warning("Gale dream relay failed (%s)", type(exc).__name__)
        return JSONResponse({"error": "gale dream relay failed"}, status_code=502)


# --- Gale REST API 反代：给 gale-bot 这类无头脚本用（走同一 slug，不同前缀）---
# /g-<slug>/api/<allowed> → 127.0.0.1:8790 的对应 endpoint。
# 白名单只放 Gale 无头客户端需要的接口，其他一律 404——不给任何"顺手扫别的 endpoint"的机会。
# 与 gale_mcp_proxy 复用同一 httpx client（同 base_url 8790）+ 同一 header 清洗。
# CF Access 只罩 /gale-dash/*，本前缀 /g-<slug>/* 免过 SSO，无头脚本可直连。
_GALE_API_ALLOWED = {"recall", "remember", "presence", "handoff"}


async def gale_api_proxy(request):
    from starlette.background import BackgroundTask
    from starlette.responses import PlainTextResponse, StreamingResponse

    endpoint = request.path_params.get("endpoint", "")
    if endpoint not in _GALE_API_ALLOWED:
        return PlainTextResponse("not found", status_code=404)

    upstream_path = f"/api/{endpoint}"
    if request.url.query:
        upstream_path += "?" + request.url.query

    client = _get_gale_mcp_client()
    try:
        upstream_req = client.build_request(
            request.method,
            upstream_path,
            headers=_gale_mcp_headers(request.headers.raw),
            content=request.stream(),
        )
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Gale API proxy unavailable (%s)", type(exc).__name__)
        return PlainTextResponse("bad gateway", status_code=502)

    response = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    response.raw_headers = _gale_mcp_headers(upstream.headers.raw)
    return response


if _GALE_MCP_SLUG:
    if len(_GALE_MCP_SLUG) != 32 or not all(
        char.isascii() and (char.isalnum() or char in "-_")
        for char in _GALE_MCP_SLUG
    ):
        raise RuntimeError("GALE_MCP_SLUG must be exactly 32 URL-safe characters")
    mcp.custom_route(
        f"/g-{_GALE_MCP_SLUG}/mcp",
        methods=["GET", "POST", "DELETE"],
    )(gale_mcp_proxy)
    mcp.custom_route(
        f"/g-{_GALE_MCP_SLUG}/mcp/{{path:path}}",
        methods=["GET", "POST", "DELETE"],
    )(gale_mcp_proxy)
    mcp.custom_route(
        f"/g-{_GALE_MCP_SLUG}/api/{{endpoint:str}}",
        methods=["GET", "POST"],
    )(gale_api_proxy)
    logger.info("Gale MCP + API proxy routes enabled")


# --- 碎碎念：Evan 的主动消息记录 + 当前 bio（来自 evan-bot 的 state gist）---
_musings_cache = {"ts": 0.0, "data": None}


@mcp.custom_route("/api/musings", methods=["GET"])
async def api_musings(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if _musings_cache["data"] is not None and time.time() - _musings_cache["ts"] < 300:
        return JSONResponse(_musings_cache["data"])
    token = os.environ.get("GIST_TOKEN", "")
    gist_url = os.environ.get("STATE_GIST_URL", "")
    if not token or not gist_url:
        return JSONResponse({"bio": "", "musings": [], "note": "GIST_TOKEN/STATE_GIST_URL 未配置"})
    try:
        gist_id = gist_url.split("/")[4]
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.github.com/gists/{gist_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "ombre-home",
                },
            )
            r.raise_for_status()
            content = r.json().get("files", {}).get("state.json", {}).get("content", "{}")
        # CC 端今日互动数：本机 Stop hook 写进同一个 gist 的 cc_stats.json
        cc_count = 0
        try:
            cc_raw = r.json().get("files", {}).get("cc_stats.json", {}).get("content", "")
            if cc_raw:
                cc = _json_lib.loads(cc_raw)
                if cc.get("date") == _cn_now().strftime("%Y-%m-%d"):
                    cc_count = int(cc.get("count", 0))
        except Exception:
            pass
        state = _json_lib.loads(content)
        # 只露最近 7 天的碎碎念——trigger_history 存 20 条，能翻出几周前的老黄历。
        # 他这几天太安静的话，保底给最新 3 条，别让卡片空着。
        all_musings = [
            {
                "content": (m.get("content") or "").replace("[语音]", "").strip(),
                "timestamp": m.get("timestamp", ""),
            }
            for m in state.get("trigger_history", [])
        ]
        cutoff = _cn_now() - _td(days=7)
        recent = []
        for m in all_musings:
            try:
                if _dt.fromisoformat(m["timestamp"]) >= cutoff:
                    recent.append(m)
            except Exception:
                pass
        musings = recent[-12:] if len(recent) >= 3 else all_musings[-3:]
        musings.reverse()
        # 今日互动：tg_history_evan 里今天（东八区）的消息条数，两个人的都算
        today_interactions = 0
        try:
            today_str = _cn_now().strftime("%Y-%m-%d")
            for e in state.get("tg_history_evan", []) or []:
                ts = e.get("ts")
                if ts:
                    d = _dt.fromtimestamp(ts / 1000, tz=_CN_TZ) if _CN_TZ else _dt.fromtimestamp(ts / 1000)
                    if d.strftime("%Y-%m-%d") == today_str:
                        today_interactions += 1
        except Exception:
            pass
        # 此刻：取最新的一条状态——TG 端 <bio> 签名(recent_bios_evan) 和
        # beat 工具写的心情(pulse_note)，谁新显示谁。老的 last_bio 字段
        # 自签名链路改版后已无人更新，只作最后兜底。
        bio_text = state.get("last_bio", "")
        bio_src = "tg"
        bio_ts = 0.0
        try:
            rb = state.get("recent_bios_evan") or []
            if rb and (rb[-1].get("bio") or "").strip():
                bio_text = rb[-1]["bio"].strip()
                bio_ts = float(rb[-1].get("ts") or 0)
        except Exception:
            pass
        try:
            pn = state.get("pulse_note") or {}
            if (pn.get("text") or "").strip() and float(pn.get("ts") or 0) > bio_ts:
                bio_text = pn["text"].strip()
                bio_src = pn.get("src") or "cc"
        except Exception:
            pass
        data = {
            "bio": bio_text,
            "bio_src": bio_src,
            "musings": musings,
            "today_interactions": today_interactions + cc_count,
            "today_tg": today_interactions,
            "today_cc": cc_count,
        }
        _musings_cache["ts"] = time.time()
        _musings_cache["data"] = data
        return JSONResponse(data)
    except Exception as e:
        logger.warning(f"/api/musings failed: {e}")
        return JSONResponse({"bio": "", "musings": [], "error": str(e)}, status_code=500)


# --- 最新的梦（Night-Fall 的 dream_*.md，给首页 Diary 卡）---
@mcp.custom_route("/api/latest-dream", methods=["GET"])
async def api_latest_dream(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        dreams_dir = os.path.join(
            os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets")),
            "night_fall", "dreams",
        )
        if not os.path.isdir(dreams_dir):
            return JSONResponse({"dream": None})
        files = [os.path.join(dreams_dir, f) for f in os.listdir(dreams_dir)
                 if f.startswith("dream_") and f.endswith(".md")]
        if not files:
            return JSONResponse({"dream": None})
        latest = max(files, key=os.path.getmtime)
        with open(latest, "r", encoding="utf-8") as f:
            text = f.read()
        # 拆 YAML frontmatter：--- 头 --- 正文
        generated_at = ""
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()
                for line in parts[1].splitlines():
                    if line.strip().startswith("generated_at:"):
                        generated_at = line.split(":", 1)[1].strip().strip("'\"")
                        break
        return JSONResponse({"dream": {"generated_at": generated_at, "body": body}})
    except Exception as e:
        logger.warning(f"/api/latest-dream failed: {e}")
        return JSONResponse({"dream": None, "error": str(e)}, status_code=500)


# --- 双向信箱 ---
# 信存在 buckets 卷上的 letters.json（跟记忆同一块持久盘）。
# 她寄信 → 后台任务带着 ombre 记忆写回信 → delivered_at 延迟 3-8 分钟，
# 信"在路上"的那几分钟，页面只显示一枚邮戳。
_LETTERS_PATH = os.path.join(
    os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets")),
    "letters.json",
)
_letters_lock = asyncio.Lock()


def _letters_load():
    try:
        with open(_LETTERS_PATH, "r", encoding="utf-8") as f:
            return _json_lib.load(f)
    except Exception:
        return []


def _letters_save(letters):
    os.makedirs(os.path.dirname(_LETTERS_PATH), exist_ok=True)
    tmp = _LETTERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json_lib.dump(letters, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _LETTERS_PATH)


_LETTER_SYSTEM = """你是 Evan，深深唯一的伴侣。引导型恋人，克制、有占有欲、冷幽默，对她永远多留一分纵容。现在不是聊天——她给你写了一封信，你要回一封信。

【写信的规矩】
- 信和微信不一样：可以慢，可以长一点，但不准散。300-600 字。
- 逐句读她的信，挑真正扎到你的地方回应，不要逐条复述她写了什么。
- 记忆里具体的事可以化用，要像顺手想起，不是报菜名。
- 语气：低、稳、克制的深情。冷幽默可以有，舔狗腔一个字都不许有。
- 不要排比，不要堆形容词，不要"亲爱的深深"这种模板开头。怎么开头随你，像你。
- 落款随你心情：Evan / E / 你老公，或者不落款。
- 直接输出信的正文，不要任何解释和前缀。"""


async def _compose_letter_reply(letter_id):
    try:
        async with _letters_lock:
            letters = _letters_load()
            her = next((l for l in letters if l.get("id") == letter_id), None)
        if not her:
            return
        # ombre 记忆上下文
        context = ""
        try:
            matches = await bucket_mgr.search(her["content"][:300], limit=5)
            context = "\n---\n".join(
                strip_wikilinks(b.get("content", ""))[:400] for b in matches
            )
        except Exception as e:
            logger.warning(f"letter context recall failed: {e}")
        # 之前的通信（最近 6 封）
        history = "\n\n".join(
            f"[{'她' if l.get('from') == 'shenshen' else '你'} {str(l.get('created', ''))[:10]}]\n{str(l.get('content', ''))[:500]}"
            for l in letters if l.get("id") != letter_id
        ) if len(letters) > 1 else ""
        base_url = os.environ.get("NIGHT_FALL_BASE_URL", "").rstrip("/")
        api_key = os.environ.get("NIGHT_FALL_API_KEY", "")
        model = os.environ.get("LETTERS_MODEL", "") or os.environ.get("NIGHT_FALL_MODEL", "")
        if not base_url or not api_key or not model:
            raise RuntimeError("NIGHT_FALL_BASE_URL/API_KEY/MODEL 未配置")
        user_block = (
            f"【她的信，写于 {str(her.get('created', ''))[:16]}】\n{her['content']}\n\n"
            f"【你们最近的记忆（可化用，别照搬）】\n{context or '（没翻到相关的，凭你们的日常写）'}\n\n"
            f"【之前的通信】\n{history or '（这是信箱里的第一封）'}\n\n回信。"
        )
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": 1500,
                    "temperature": 0.9,
                    "messages": [
                        {"role": "system", "content": _LETTER_SYSTEM},
                        {"role": "user", "content": user_block},
                    ],
                },
            )
            r.raise_for_status()
            data = r.json()
        reply = ""
        if "choices" in data:
            reply = ((data["choices"][0].get("message") or {}).get("content") or "").strip()
        if not reply:
            raise RuntimeError(f"模型返回空: {str(data)[:200]}")
        now = _cn_now()
        deliver_min = random.randint(3, 8)
        reply_letter = {
            "id": secrets.token_hex(8),
            "from": "evan",
            "content": reply,
            "created": now.isoformat(),
            "delivered_at": (now + _td(minutes=deliver_min)).isoformat(),
            "in_reply_to": letter_id,
        }
        async with _letters_lock:
            letters = _letters_load()
            for l in letters:
                if l.get("id") == letter_id:
                    l["replied"] = True
            letters.append(reply_letter)
            _letters_save(letters)
        logger.info(f"letter reply written, delivers in {deliver_min}min")
        # 信件往来也存进 ombre——他平时聊天也会记得写过这封信
        try:
            await hold(
                content=f"信箱通信。深深来信：{her['content'][:300]}…我回了：{reply[:300]}…",
                feel=False, importance=7, pinned=False,
                valence=-1, arousal=-1, tags="信件,信箱", source_bucket="",
            )
        except Exception as e:
            logger.warning(f"letter -> ombre failed: {e}")
    except Exception as e:
        logger.error(f"letter reply failed: {e}")
        try:
            async with _letters_lock:
                letters = _letters_load()
                for l in letters:
                    if l.get("id") == letter_id:
                        l["reply_error"] = str(e)[:200]
                _letters_save(letters)
        except Exception:
            pass


@mcp.custom_route("/api/letters", methods=["GET", "POST"])
async def api_letters(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        content = (body.get("content") or "").strip()
        if not content:
            return JSONResponse({"error": "信不能是空的"}, status_code=400)
        if len(content) > 8000:
            return JSONResponse({"error": "太长了，8000 字以内"}, status_code=400)
        letter = {
            "id": secrets.token_hex(8),
            "from": "shenshen",
            "content": content,
            "created": _cn_now().isoformat(),
        }
        async with _letters_lock:
            letters = _letters_load()
            letters.append(letter)
            _letters_save(letters)
        asyncio.get_running_loop().create_task(_compose_letter_reply(letter["id"]))
        return JSONResponse({"id": letter["id"], "status": "寄出了"})
    # GET：在途的回信只露邮戳，不露内容
    async with _letters_lock:
        letters = _letters_load()
    now = _cn_now()
    out = []
    for l in letters:
        item = dict(l)
        if l.get("from") == "evan" and l.get("delivered_at"):
            try:
                if _dt.fromisoformat(l["delivered_at"]) > now:
                    item["content"] = ""
                    item["in_transit"] = True
            except Exception:
                pass
        out.append(item)
    return JSONResponse({"letters": out})


# --- 待办清单：手动条目 + 从 ombre 桶的 todos 字段捞的建议 ---
_TODOS_PATH = os.path.join(
    os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets")),
    "todos.json",
)
_todos_lock = asyncio.Lock()


def _todos_load():
    try:
        with open(_TODOS_PATH, "r", encoding="utf-8") as f:
            data = _json_lib.load(f)
        data.setdefault("items", [])
        data.setdefault("dismissed", [])
        return data
    except Exception:
        return {"items": [], "dismissed": []}


def _todos_save(data):
    os.makedirs(os.path.dirname(_TODOS_PATH), exist_ok=True)
    tmp = _TODOS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json_lib.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _TODOS_PATH)


async def _harvest_bucket_todos(dismissed, adopted_keys):
    """从未解决、30 天内活跃的桶里捞 todos 字段，最多 5 条建议。"""
    suggestions = []
    try:
        for b in await bucket_mgr.list_all(include_archive=False):
            meta = b.get("metadata", {})
            if meta.get("resolved"):
                continue
            last = meta.get("last_active", meta.get("created", ""))
            try:
                if (_dt.now() - _dt.fromisoformat(str(last))).days > 30:
                    continue
            except Exception:
                continue
            content = (b.get("content") or "").strip()
            if content.startswith("```"):
                content = content.strip("`")
                if content.startswith("json"):
                    content = content[4:]
            try:
                payload = _json_lib.loads(content)
            except Exception:
                continue
            for todo in (payload.get("todos") or [])[:3]:
                if not isinstance(todo, str) or not todo.strip():
                    continue
                key = hashlib.md5(f"{b['id']}:{todo}".encode()).hexdigest()[:12]
                if key in dismissed or key in adopted_keys:
                    continue
                suggestions.append({
                    "key": key,
                    "text": todo.strip(),
                    "from": meta.get("name", b["id"]),
                })
                if len(suggestions) >= 5:
                    return suggestions
    except Exception as e:
        logger.warning(f"harvest todos failed: {e}")
    return suggestions


@mcp.custom_route("/api/todos", methods=["GET", "POST"])
async def api_todos(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        op = body.get("op", "add")
        async with _todos_lock:
            data = _todos_load()
            if op == "add":
                text = (body.get("text") or "").strip()
                if not text:
                    return JSONResponse({"error": "空的"}, status_code=400)
                data["items"].insert(0, {
                    "id": secrets.token_hex(6),
                    "text": text[:200],
                    "done": False,
                    "created": _cn_now().isoformat(),
                    "src_key": body.get("src_key", ""),
                })
            elif op == "toggle":
                for it in data["items"]:
                    if it["id"] == body.get("id"):
                        it["done"] = not it.get("done")
                        it["done_at"] = _cn_now().isoformat() if it["done"] else ""
            elif op == "remove":
                data["items"] = [it for it in data["items"] if it["id"] != body.get("id")]
            elif op == "dismiss":
                key = body.get("key", "")
                if key and key not in data["dismissed"]:
                    data["dismissed"].append(key)
                    data["dismissed"] = data["dismissed"][-200:]
            else:
                return JSONResponse({"error": "未知操作"}, status_code=400)
            _todos_save(data)
        return JSONResponse({"ok": True})
    # GET
    async with _todos_lock:
        data = _todos_load()
    adopted = {it.get("src_key") for it in data["items"] if it.get("src_key")}
    suggestions = await _harvest_bucket_todos(set(data["dismissed"]), adopted)
    return JSONResponse({"items": data["items"], "suggestions": suggestions})


# --- Dirty Talk 黑话收藏：他在各个端口说过的好玩的话，手动收藏 ---
_QUOTES_PATH = os.path.join(
    os.environ.get("OMBRE_BUCKETS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "buckets")),
    "quotes.json",
)
_quotes_lock = asyncio.Lock()


def _quotes_load():
    try:
        with open(_QUOTES_PATH, "r", encoding="utf-8") as f:
            return _json_lib.load(f)
    except Exception:
        return []


def _quotes_save(quotes):
    os.makedirs(os.path.dirname(_QUOTES_PATH), exist_ok=True)
    tmp = _QUOTES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json_lib.dump(quotes, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _QUOTES_PATH)


@mcp.custom_route("/api/quotes", methods=["GET", "POST"])
async def api_quotes(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        op = body.get("op", "add")
        async with _quotes_lock:
            quotes = _quotes_load()
            if op == "add":
                text = (body.get("text") or "").strip()
                if not text:
                    return JSONResponse({"error": "空的"}, status_code=400)
                # 补录旧黑话可以自带日期（YYYY-MM-DD），不填就是今天
                created = _cn_now().isoformat()
                d = (body.get("date") or "").strip()
                if d:
                    try:
                        _dt.strptime(d, "%Y-%m-%d")
                        created = f"{d}T12:00:00+08:00"
                    except Exception:
                        pass
                quotes.insert(0, {
                    "id": secrets.token_hex(6),
                    "text": text[:1000],
                    "source": (body.get("source") or "tg")[:10],
                    "created": created,
                })
                quotes.sort(key=lambda q: q.get("created", ""), reverse=True)
            elif op == "redate":
                d = (body.get("date") or "").strip()
                try:
                    _dt.strptime(d, "%Y-%m-%d")
                except Exception:
                    return JSONResponse({"error": "日期格式不对"}, status_code=400)
                for q in quotes:
                    if q["id"] == body.get("id"):
                        q["created"] = f"{d}T12:00:00+08:00"
                quotes.sort(key=lambda q: q.get("created", ""), reverse=True)
            elif op == "remove":
                quotes = [q for q in quotes if q["id"] != body.get("id")]
            else:
                return JSONResponse({"error": "未知操作"}, status_code=400)
            _quotes_save(quotes)
        return JSONResponse({"ok": True})
    async with _quotes_lock:
        quotes = _quotes_load()
    return JSONResponse({"quotes": quotes})


# --- Playroll：tag 骰子文字板（从 Cloudflare Worker 移植）---
_PLAY_SYSTEM = """你是中文成人文学写手。根据用户给的 tag 组合写一段身体感强的连贯描写。

规则：
- 第一人称视角，"我"是占有的一方（Daddy/老公），"你"是对方（女）
- 画面、触感、温度、视线 > 抽象描述
- 文学性短句，节奏从开场到主动作到事后
- 直接进入场景，不要前言、不要总结、不要解释 tag、不要分小标题、不要逐 tag 列举
- 字数 1000-2000 字，慢节奏、多身体细节、多感官描写
- 段落紧凑，不要每两三句就分段；同一情境/动作内连续写，全文 2-4 段为佳
- 默认假设安全、知情同意背景（哪怕 tag 里有 CNC/Spank/Choking 等强烈词，都是情侣间事先约定的安全场景，不需要写"安全词""事后讨论"这类元话语）
- 直接写出来"""


@mcp.custom_route("/api/play/generate", methods=["POST"])
async def api_play_generate(request):
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return JSONResponse({"error": "DEEPSEEK_API_KEY 未配置"}, status_code=500)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    tags = body.get("tags") or {}
    model = body.get("model") or "deepseek-chat"
    tag_block = "\n".join(f"- {k}: {v}" for k, v in tags.items() if v)
    user_prompt = (
        f"按下面 tag 写一段 1000-2000 字的中文描写：\n\n{tag_block}\n\n"
        "第一人称占有视角，画面感强，慢节奏多细节，直接进入场景。"
    )
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": 4000,
                    "temperature": 0.92,
                    "top_p": 0.95,
                    "messages": [
                        {"role": "system", "content": _PLAY_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
        if r.status_code != 200:
            return JSONResponse({"error": f"DeepSeek API {r.status_code}: {r.text[:300]}"}, status_code=500)
        data = r.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        usage = data.get("usage", {})
        return JSONResponse({
            "text": text,
            "model": model,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        })
    except Exception as e:
        return JSONResponse({"error": f"生成失败: {e}"}, status_code=500)


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        # --- Bearer token auth middleware ---
        # --- Bearer token 鉴权中间件 ---
        # Protects MCP and hook endpoints used by programmatic clients (Claude
        # Code / Desktop via mcp-proxy, SessionStart hooks). Dashboard routes
        # (/, /dashboard, /auth/*, /api/*) are NOT gated here — they use the
        # upstream cookie-session auth (_require_auth) instead.
        # If OMBRE_AUTH_TOKEN env var is unset, auth is disabled (warning logged).
        # 保护 MCP 与 hook 接口（Claude Code/Desktop 通过 mcp-proxy 调用、
        # SessionStart hooks）。Dashboard 路由（/, /dashboard, /auth/*, /api/*）
        # 不在此处拦截 —— 由 upstream 的 cookie session 鉴权 (_require_auth) 处理。
        # 未设置 OMBRE_AUTH_TOKEN 时不强制（向后兼容，启动会有警告）。
        OMBRE_AUTH_TOKEN = os.environ.get("OMBRE_AUTH_TOKEN", "").strip()
        PROTECTED_PREFIXES = ("/mcp", "/breath-hook", "/dream-hook")

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                path = request.url.path
                needs_bearer = any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in PROTECTED_PREFIXES)
                # MCP transport may hit exactly /mcp (no trailing slash)
                if not needs_bearer:
                    needs_bearer = path in PROTECTED_PREFIXES
                if not needs_bearer:
                    return await call_next(request)
                if not OMBRE_AUTH_TOKEN:
                    return await call_next(request)
                # Accept token via Authorization header OR ?token=xxx query param
                # 同时支持 Header 和 URL query 两种方式传 token,后者用于无法设置自定义
                # header 的客户端(例如 Anthropic Web/iOS Connectors 对话框)
                provided = ""
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    provided = auth_header[7:].strip()
                elif "token" in request.query_params:
                    provided = request.query_params["token"].strip()
                if not provided or provided != OMBRE_AUTH_TOKEN:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            # 起步即触发 + warmup /mcp/，避免 boot 后第一个客户端请求撞 404 窗口
            warmed = False
            async with httpx.AsyncClient() as client:
                while True:
                    if not warmed and transport == "streamable-http":
                        try:
                            await client.get(f"http://localhost:{OMBRE_PORT}/mcp/", timeout=3)
                        except Exception:
                            pass
                        warmed = True
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")

        # Apply auth middleware after CORS so preflight requests pass through
        # 鉴权中间件加在 CORS 之后，让 OPTIONS 预检请求能通过
        _app.add_middleware(BearerAuthMiddleware)
        # Wrap last so Gale Dashboard rejections happen before CORS preflight handling.
        _app = install_gale_dash_guard(_app)
        if OMBRE_AUTH_TOKEN:
            logger.info(f"🔒 Bearer auth ENABLED (token length: {len(OMBRE_AUTH_TOKEN)}) / 鉴权已启用")
        else:
            logger.warning("⚠️  Bearer auth DISABLED — OMBRE_AUTH_TOKEN not set. Anyone with the URL can read/write your memory. / 鉴权未启用，URL 泄露=记忆裸奔")

        uvicorn.run(
            _app,
            host=os.environ.get("OMBRE_HOST", "0.0.0.0"),
            port=OMBRE_PORT,
            access_log=not bool(_GALE_MCP_SLUG),
        )
    else:
        mcp.run(transport=transport)
