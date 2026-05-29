"""
shiyi-shell Web Chat UI — 多对话窗口 + 文件上传下载
Developed by LiGuo LeGang
Licensed under MIT License

启动：python -m shiyi.shell.webui
或：  shiyi webui
"""

import sys
import asyncio
import os
import shutil
import uuid
import sqlite3
import datetime
import json
from pathlib import Path

# ═══ 冻结算检测 ═══
_IS_FROZEN = getattr(sys, 'frozen', False)

# ═══ 加载 .env 文件 ═══
if _IS_FROZEN:
    _ENV_PATHS = [Path(sys.executable).parent.parent / ".env"]
else:
    _ENV_PATHS = [
        Path(__file__).parent.parent.parent.parent / ".env",
        Path.home() / ".shiyi" / ".env",
    ]
for env_path in _ENV_PATHS:
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    if key.strip() and val:
                        val = val.strip()
                        if val.startswith('"') and val.endswith('"'):
                            val = val[1:-1]
                        elif val.startswith("'") and val.endswith("'"):
                            val = val[1:-1]
                        os.environ[key.strip()] = val
        break

try:
    from fastapi import FastAPI, HTTPException, UploadFile, File
    from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    print("需要安装依赖: pip install fastapi uvicorn python-multipart")
    sys.exit(1)

from shiyi.engine import Shiyi
from shiyi.shell.llm_caller import create_llm_caller
from shiyi.shell.embedding_caller import create_embedding_caller
from shiyi.shell import __version__
from shiyi.common.errors import LLMUnavailableError
from shiyi.common.constants import (
    DEFAULT_MAIN_LLM_MODEL,
    DEFAULT_LIGHT_LLM_MODEL,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_BASE_URL,
)

# ═══════════════════════════════════════════
# 数据库初始化 (sessions + messages)
# ═══════════════════════════════════════════

DATA_DIR = Path.home() / ".shiyi" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "sessions.db"
WORKSPACE_DIR = Path.home() / ".shiyi" / "workspace"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
MAX_SESSIONS = 10
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

def _init_db():
    """初始化 sessions 和 messages 表"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(session_id, created_at)")
    conn.commit()

    # 确保至少有一个默认会话
    cur = conn.execute("SELECT COUNT(*) FROM sessions")
    count = cur.fetchone()[0]
    if count == 0:
        now = datetime.datetime.utcnow().isoformat()
        sid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sessions (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, "对话1", now, now)
        )
        conn.commit()
    conn.close()

def _get_db() -> sqlite3.Connection:
    """获取数据库连接（自动启用 WAL + 外键）"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

_init_db()

# ═══════════════════════════════════════════
# 初始化引擎
# ═══════════════════════════════════════════

app = FastAPI(title="史佚 ShiYi - Web Chat")
STATIC_DIR = Path(__file__).parent / "static"

_shiyi: Shiyi = None

def get_shiyi() -> Shiyi:
    global _shiyi
    if _shiyi is None:
        try:
            llm = create_llm_caller()
        except Exception:
            llm = None
        try:
            embedding = create_embedding_caller()
        except Exception:
            embedding = None
        _shiyi = Shiyi(llm_provider=llm, embedding_provider=embedding)

        # ═══ 吏员系统: v0.13.0 远程吏员（MCP subprocess） ═══
        try:
            clerk_path = Path(__file__).parent / "clerk-default"
            mcp_script = clerk_path / "mcp_server.py"
            if mcp_script.exists():
                from shiyi.core.clerk_connector import RemoteClerk
                remote_clerk = RemoteClerk(
                    server_script=str(mcp_script),
                    config_path=str(clerk_path / "clerk.json"),
                )
                _shiyi.clerk_registry.register_clerk(remote_clerk)
                print(f"远程吏员已注册: {remote_clerk.config.clerk_id}")
            else:
                import sys
                sys.path.insert(0, str(clerk_path))
                from worker import ClerkWorker
                local_clerk = ClerkWorker(str(clerk_path / "clerk.json"))
                _shiyi.clerk_registry.register_clerk(local_clerk)
                print(f"本地吏员已注册: {local_clerk.config.clerk_id}")
        except Exception as e:
            print(f"吏员注册失败（无工具模式）: {e}")

        # ═══ 管家 LLM 回调初始化 (v0.19.0 Phase 3) ═══
        if llm:
            llm_fn = lambda msgs: llm.chat(msgs, temperature=0.3, max_tokens=4000)
            _shiyi.set_steward_llm(llm_fn)

    return _shiyi

# ═══════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""  # 空则使用默认会话

class ChatResponse(BaseModel):
    reply: str
    status: str = "ok"

class ConfigRequest(BaseModel):
    main_key: str = ""
    embedding_key: str = ""
    main_model: str = DEFAULT_MAIN_LLM_MODEL
    main_base_url: str = DEFAULT_LLM_BASE_URL
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    light_model: str = DEFAULT_LIGHT_LLM_MODEL
    light_key: str = ""
    light_base_url: str = ""
    fallback_model: str = ""
    fallback_key: str = ""
    fallback_base_url: str = ""

class ChannelConfigRequest(BaseModel):
    channel: str  # "feishu", "wechat", etc.
    config: dict  # key-value pairs for the channel

class SessionRename(BaseModel):
    name: str

# ═══════════════════════════════════════════
# Session API
# ═══════════════════════════════════════════

def _get_default_session_id() -> str:
    """获取默认会话 ID（取更新时间最近的）"""
    conn = _get_db()
    try:
        cur = conn.execute("SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            return row["id"]
    finally:
        conn.close()
    # 极端情况：表为空 → 创建默认
    now = datetime.datetime.utcnow().isoformat()
    sid = str(uuid.uuid4())
    conn2 = _get_db()
    try:
        conn2.execute(
            "INSERT INTO sessions (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, "对话1", now, now)
        )
        conn2.commit()
    finally:
        conn2.close()
    return sid

def _resolve_session(session_id: str) -> str:
    """解析 session_id：空则返回默认；不存在则自动创建"""
    if not session_id or not session_id.strip():
        return _get_default_session_id()
    session_id = session_id.strip()
    # 验证 session 存在，不存在则创建
    conn = _get_db()
    try:
        cur = conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,))
        if cur.fetchone() is None:
            now = datetime.datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO sessions (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, "对话" + session_id[:4], now, now)
            )
            conn.commit()
    finally:
        conn.close()
    return session_id

@app.get("/api/sessions")
async def list_sessions():
    """列出所有会话"""
    conn = _get_db()
    try:
        cur = conn.execute("SELECT id, name, created_at, updated_at FROM sessions ORDER BY updated_at DESC")
        rows = cur.fetchall()
        sessions = []
        for row in rows:
            # 附带消息数
            msg_cur = conn.execute("SELECT COUNT(*) as cnt FROM messages WHERE session_id=?", (row["id"],))
            msg_cnt = msg_cur.fetchone()["cnt"]
            sessions.append({
                "id": row["id"],
                "name": row["name"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "message_count": msg_cnt,
            })
        return {"sessions": sessions}
    finally:
        conn.close()

@app.post("/api/sessions")
async def create_session():
    """新建会话（最多 MAX_SESSIONS 个）"""
    conn = _get_db()
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM sessions")
        count = cur.fetchone()["cnt"]
        if count >= MAX_SESSIONS:
            raise HTTPException(status_code=400, detail=f"已达到上限({MAX_SESSIONS}个对话窗口)")
        # 自动编号
        cur2 = conn.execute("SELECT name FROM sessions ORDER BY created_at")
        used_numbers = set()
        for row in cur2.fetchall():
            if row["name"].startswith("对话"):
                try:
                    used_numbers.add(int(row["name"][2:]))
                except ValueError:
                    pass
        n = 1
        while n in used_numbers:
            n += 1
        name = f"对话{n}"
        now = datetime.datetime.utcnow().isoformat()
        sid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO sessions (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sid, name, now, now)
        )
        conn.commit()
        return {"id": sid, "name": name, "created_at": now}
    finally:
        conn.close()

@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    """删除会话（禁止删最后一个）"""
    conn = _get_db()
    try:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM sessions")
        count = cur.fetchone()["cnt"]
        if count <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个对话窗口")
        cur2 = conn.execute("SELECT id FROM sessions WHERE id=?", (sid,))
        if not cur2.fetchone():
            raise HTTPException(status_code=404, detail="会话不存在")
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()

@app.put("/api/sessions/{sid}")
async def rename_session(sid: str, body: SessionRename):
    """重命名会话"""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="名称不能为空")
    conn = _get_db()
    try:
        cur = conn.execute("SELECT id FROM sessions WHERE id=?", (sid,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="会话不存在")
        now = datetime.datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE sessions SET name=?, updated_at=? WHERE id=?",
            (name, now, sid)
        )
        conn.commit()
        return {"status": "ok", "name": name}
    finally:
        conn.close()

@app.get("/api/sessions/{sid}/messages")
async def get_session_messages(sid: str, limit: int = 200, before: str = ""):
    """加载会话历史消息"""
    conn = _get_db()
    try:
        if before:
            cur = conn.execute(
                "SELECT id, role, content, created_at FROM messages WHERE session_id=? AND created_at < ? ORDER BY created_at ASC LIMIT ?",
                (sid, before, limit)
            )
        else:
            cur = conn.execute(
                "SELECT id, role, content, created_at FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ?",
                (sid, limit)
            )
        rows = cur.fetchall()
        messages = [
            {"id": row["id"], "role": row["role"], "content": row["content"], "created_at": row["created_at"]}
            for row in rows
        ]
        return {"messages": messages}
    finally:
        conn.close()

# ═══════════════════════════════════════════
# 文件上传/下载 API
# ═══════════════════════════════════════════

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传文件到 workspace"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名为空")
    # 安全检查：拒绝路径穿越
    safe_name = Path(file.filename).name
    if not safe_name or safe_name != file.filename:
        raise HTTPException(status_code=400, detail="文件名不合法")

    dest = WORKSPACE_DIR / safe_name
    # 读取并检查大小
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大（上限 {MAX_UPLOAD_SIZE // 1024 // 1024} MB）")

    with open(dest, "wb") as f:
        f.write(content)
    return {"status": "ok", "filename": safe_name, "size": len(content)}

@app.get("/api/download")
async def download_file(path: str = ""):
    """下载 workspace 中的文件"""
    if not path:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    safe_name = Path(path).name
    file_path = WORKSPACE_DIR / safe_name
    # 安全检查：确保解析后在 workspace 内
    try:
        file_path = file_path.resolve()
        WORKSPACE_DIR.resolve()
        if not str(file_path).startswith(str(WORKSPACE_DIR.resolve())):
            raise HTTPException(status_code=403, detail="路径不允许")
    except Exception:
        raise HTTPException(status_code=403, detail="路径不允许")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    return FileResponse(str(file_path), filename=safe_name)

@app.get("/api/workspace-files")
async def list_workspace_files():
    """列出 workspace 中的文件"""
    files = []
    try:
        for f in sorted(WORKSPACE_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file():
                files.append({"name": f.name, "size": f.stat().st_size})
    except Exception:
        pass
    return {"files": files}

# ═══════════════════════════════════════════
# 对话 API
# ═══════════════════════════════════════════

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """对话接口（带 session_id 存储）"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = _resolve_session(req.session_id)
    now = datetime.datetime.utcnow().isoformat()

    # 保存用户消息
    conn = _get_db()
    try:
        user_msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_msg_id, session_id, "user", req.message, now)
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        conn.commit()
    finally:
        conn.close()

    try:
        shiyi = get_shiyi()
        if not shiyi.llm_available:
            reply_text = (
                "⚠️ LLM服务不可用，对话无法进行。\n"
                "请检查网络连接和API配置（DEEPSEEK_API_KEY或SILICONFLOW_API_KEY）。\n"
                "点击右上角齿轮进入设置。"
            )
            status = "llm_unavailable"
        else:
            platform_ctx = _build_platform_context(shiyi)
            reply_text = await asyncio.to_thread(
                shiyi.chat, req.message, platform_context=platform_ctx
            )
            status = "ok"
    except LLMUnavailableError as e:
        reply_text = (
            f"⚠️ {str(e)}\n"
            "请检查网络连接和API配置后重试。"
        )
        status = "llm_unavailable"
    except Exception as e:
        reply_text = f"错误: {e}"
        status = "error"

    # 保存史佚回复
    conn2 = _get_db()
    try:
        reply_msg_id = str(uuid.uuid4())
        conn2.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (reply_msg_id, session_id, "shiyi", reply_text, datetime.datetime.utcnow().isoformat())
        )
        conn2.execute("UPDATE sessions SET updated_at=? WHERE id=?", (datetime.datetime.utcnow().isoformat(), session_id))
        conn2.commit()
    finally:
        conn2.close()

    return ChatResponse(reply=reply_text, status=status)


# ═══ 会话消息清空 ═══

@app.delete("/api/sessions/{sid}/messages")
async def delete_session_messages(sid: str):
    """清空指定会话的全部消息"""
    conn = _get_db()
    try:
        conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        now = datetime.datetime.utcnow().isoformat()
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, sid))
        conn.commit()
        return {"status": "ok"}
    finally:
        conn.close()


# ═══════════════════════════════════════════
# 健康检查 / 配置 / 异步 / 看板 (保持原有)
# ═══════════════════════════════════════════

@app.get("/api/health")
async def health():
    shiyi = get_shiyi()
    return {
        "status": "ok",
        "llm_available": shiyi.llm_available,
        "embedding_available": shiyi.embedding_available,
        "version": __version__
    }

class AsyncChatResponse(BaseModel):
    mode: str
    reply: str
    tasks: list = []
    task_summary: list = []

@app.post("/api/chat/async", response_model=AsyncChatResponse)
async def chat_async(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    import json
    try:
        shiyi = get_shiyi()
        platform_ctx = _build_platform_context(shiyi)
        result_str = await asyncio.to_thread(
            shiyi.chat_async, req.message, platform_context=platform_ctx
        )
        result = json.loads(result_str)
        return AsyncChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/task/{task_id}")
async def task_status(task_id: str):
    shiyi = get_shiyi()
    return shiyi.task_status(task_id)

@app.get("/api/kanban")
async def kanban_status():
    shiyi = get_shiyi()
    return shiyi.task_status()

# ═══════════════════════════════════════════
# 吏员管理 API (v0.15.0)
# ═══════════════════════════════════════════

CLERK_DIRS = [
    Path(__file__).parent / "clerk-default",  # 内置吏员
]

USER_CLERKS_DIR = Path.home() / ".shiyi" / "clerks"
USER_CLERKS_DIR.mkdir(parents=True, exist_ok=True)

def _scan_clerks() -> list[dict]:
    """扫描内置和用户安装的吏员目录"""
    results = []
    seen_ids = set()

    # 1. 内置吏员（项目 clerk-* 目录，排除 template）
    _EXCLUDED_CLERK_DIRS = {"clerk-template", "clerk_template"}
    project_clerks = Path(__file__).parent
    for entry in sorted(project_clerks.iterdir()):
        if entry.is_dir() and (entry.name.startswith("clerk-") or entry.name.startswith("clerk_")) and entry.name not in _EXCLUDED_CLERK_DIRS:
            info = _read_clerk_info(entry)
            if info and info["clerk_id"] not in seen_ids:
                info["_source"] = "builtin"
                info["_path"] = str(entry)
                _merge_user_config(info)
                results.append(info)
                seen_ids.add(info["clerk_id"])

    # 2. 用户安装的吏员（支持 clerk-xxx 和 clerk_xxx 两种格式）
    if USER_CLERKS_DIR.exists():
        for entry in sorted(USER_CLERKS_DIR.iterdir()):
            if entry.is_dir() and (entry.name.startswith("clerk-") or entry.name.startswith("clerk_")) and entry.name not in _EXCLUDED_CLERK_DIRS:
                info = _read_clerk_info(entry)
                if info and info["clerk_id"] not in seen_ids:
                    info["_source"] = "user"
                    info["_path"] = str(entry)
                    _merge_user_config(info)
                    results.append(info)
                    seen_ids.add(info["clerk_id"])

    return results

def _read_clerk_info(dir_path: Path) -> dict | None:
    """读取吏员目录的 clerk.json"""
    config_file = dir_path / "clerk.json"
    if not config_file.exists():
        return None
    try:
        with open(config_file) as f:
            return json.load(f)
    except Exception:
        return None

def _get_clerk_user_config_path(clerk_id: str) -> Path:
    """获取吏员用户配置文件的路径"""
    return USER_CLERKS_DIR / f"{clerk_id}.json"

def _read_user_config(clerk_id: str) -> dict:
    """读取吏员的用户配置"""
    path = _get_clerk_user_config_path(clerk_id)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _write_user_config(clerk_id: str, config: dict):
    """写入吏员的用户配置"""
    path = _get_clerk_user_config_path(clerk_id)
    with open(path, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def _merge_user_config(clerk_info: dict):
    """将用户配置合并到吏员信息中"""
    clerk_id = clerk_info.get("clerk_id", "")
    user_cfg = _read_user_config(clerk_id)

    # 启用状态：用户配置优先
    if "enabled" in user_cfg:
        clerk_info["enabled"] = user_cfg["enabled"]

    # API keys 状态（只返回是否已配置，不暴露值）
    declared_keys = clerk_info.get("api_keys", [])
    saved_keys = user_cfg.get("api_keys", {})
    clerk_info["_key_status"] = {}
    for key_entry in declared_keys:
        # api_keys in clerk.json can be [str] or [{name, description, required}]
        key_name = key_entry["name"] if isinstance(key_entry, dict) else key_entry
        clerk_info["_key_status"][key_name] = bool(saved_keys.get(key_name))

class ClerkConfigSave(BaseModel):
    api_keys: dict = {}   # {KEY_NAME: "sk-xxx"}
    base_url: str = ""    # API base URL
    model: str = ""       # 模型名称

@app.get("/api/clerks")
async def list_clerks():
    """列出所有已安装的吏员"""
    clerks = _scan_clerks()
    try:
        shiyi = get_shiyi()
        for c in clerks:
            try:
                health = shiyi.steward_clerk_health(c["clerk_id"])
                c["health"] = health.get("status", "unknown")
                c["health_detail"] = health.get("detail", {})
            except Exception:
                c["health"] = "unknown"
                c["health_detail"] = {}
    except Exception:
        pass
    return {"clerks": clerks}

@app.get("/api/clerks/{clerk_id}")
async def get_clerk_detail(clerk_id: str):
    """获取单个吏员完整信息（含 skill 清单）"""
    clerks = _scan_clerks()
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            # 读取 skill 清单
            clerk_path = Path(c["_path"])
            skills_dir = clerk_path / "skills"
            c["_skill_files"] = []
            if skills_dir.exists():
                for sf in sorted(skills_dir.glob("*.md")):
                    c["_skill_files"].append(sf.name)
            # 读取知识库文件
            kb_dir_name = c.get("knowledge_base", "knowledge/")
            kb_dir = clerk_path / kb_dir_name
            c["_kb_files"] = []
            if kb_dir.exists():
                for kf in sorted(kb_dir.iterdir()):
                    if kf.is_file():
                        c["_kb_files"].append(kf.name)
            return c
    raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")

@app.post("/api/clerks/{clerk_id}/config")
async def save_clerk_config(clerk_id: str, req: ClerkConfigSave):
    """保存吏员完整配置：API key + base_url + model"""
    clerks = _scan_clerks()
    found = None
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            found = c
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")

    user_cfg = _read_user_config(clerk_id)
    if "api_keys" not in user_cfg:
        user_cfg["api_keys"] = {}

    # 保存 API keys
    declared_keys = found.get("api_keys", [])
    declared_key_names = [k["name"] if isinstance(k, dict) else k for k in declared_keys]
    for key_name, key_value in req.api_keys.items():
        if key_name in declared_key_names and key_value.strip():
            user_cfg["api_keys"][key_name] = key_value.strip()

    # 保存 base_url 和 model
    if req.base_url:
        user_cfg["base_url"] = req.base_url
    if req.model:
        user_cfg["model"] = req.model

    _write_user_config(clerk_id, user_cfg)
    
    # 热更新：如果吏员正在运行，更新其配置
    try:
        shiyi = get_shiyi()
        clerk = shiyi.clerk_registry.get_clerk(clerk_id)
        if clerk and hasattr(clerk, 'config'):
            if req.base_url:
                clerk.config.base_url = req.base_url
            if req.model:
                clerk.config.model = req.model
    except Exception:
        pass

    return {"status": "ok", "message": f"吏员 {clerk_id} 配置已保存"}

@app.post("/api/clerks/{clerk_id}/enable")
async def enable_clerk(clerk_id: str):
    """启用吏员"""
    clerks = _scan_clerks()
    found = any(c.get("clerk_id") == clerk_id for c in clerks)
    if not found:
        raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")

    user_cfg = _read_user_config(clerk_id)
    user_cfg["enabled"] = True
    _write_user_config(clerk_id, user_cfg)

    # 热启用：注册到运行中的引擎
    try:
        shiyi = get_shiyi()
        _register_clerk_by_id(shiyi, clerk_id)
    except Exception as e:
        print(f"吏员 {clerk_id} 热启用失败: {e}")

    return {"status": "ok", "message": f"吏员 {clerk_id} 已启用"}

@app.post("/api/clerks/{clerk_id}/disable")
async def disable_clerk(clerk_id: str):
    """禁用吏员"""
    clerks = _scan_clerks()
    found = any(c.get("clerk_id") == clerk_id for c in clerks)
    if not found:
        raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")

    user_cfg = _read_user_config(clerk_id)
    user_cfg["enabled"] = False
    _write_user_config(clerk_id, user_cfg)

    # 热禁用：从运行中的引擎注销
    try:
        shiyi = get_shiyi()
        if hasattr(shiyi, 'clerk_registry'):
            shiyi.clerk_registry.unregister_clerk(clerk_id)
    except Exception as e:
        print(f"吏员 {clerk_id} 热禁用失败: {e}")

    return {"status": "ok", "message": f"吏员 {clerk_id} 已禁用"}


@app.post("/api/clerks/{clerk_id}/restart")
async def restart_clerk(clerk_id: str):
    """重启吏员 MCP 进程"""
    clerks = _scan_clerks()
    found = any(c.get("clerk_id") == clerk_id for c in clerks)
    if not found:
        raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")
    
    try:
        shiyi = get_shiyi()
        # 先停止再启动
        try:
            shiyi.steward_stop_clerk(clerk_id)
        except Exception:
            pass
        result = shiyi.steward_start_clerk(clerk_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启失败: {str(e)}")


@app.get("/api/clerks/{clerk_id}/health")
async def check_clerk_health(clerk_id: str):
    """检查吏员健康状态"""
    clerks = _scan_clerks()
    found = any(c.get("clerk_id") == clerk_id for c in clerks)
    if not found:
        raise HTTPException(status_code=404, detail=f"吏员 {clerk_id} 未找到")
    
    try:
        shiyi = get_shiyi()
        return shiyi.steward_clerk_health(clerk_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"健康检查失败: {str(e)}")

def _register_clerk_by_id(shiyi: Shiyi, clerk_id: str):
    """根据 clerk_id 注册吏员到引擎"""
    clerks = _scan_clerks()
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            clerk_path = Path(c["_path"])
            mcp_script = clerk_path / "mcp_server.py"
            if mcp_script.exists():
                from shiyi.core.clerk_connector import RemoteClerk
                remote_clerk = RemoteClerk(
                    server_script=str(mcp_script),
                    config_path=str(clerk_path / "clerk.json"),
                )
                shiyi.clerk_registry.register_clerk(remote_clerk)
                print(f"吏员已热注册: {clerk_id}")
            return

# ═══════════════════════════════════════════
# 吏员创建/删除/更名/技能分配 API
# ═══════════════════════════════════════════

class ClerkCreateReq(BaseModel):
    name: str
    description: str = ""
    tools: list = []  # [{name, description, inputSchema}]
    requires_llm: bool = False
    api_keys: list = []  # ["KEY_NAME", ...]
    model_name: str = ""  # LLM模型名称
    provider: str = ""  # LLM provider (deepseek, openai...)
    base_url: str = ""  # API base URL


def _build_platform_context(shiyi) -> str:
    """构建平台上下文字串，注入吏员能力信息到 System Prompt
    
    格式：每条「工具名: 吏员名 - 描述」，LLM 据此决定何时调用哪个吏员。
    """
    clerks = shiyi.clerk_registry.list_clerks()
    if not clerks:
        return ""

    lines = ["\n## 可用的史佚吏员"]
    for c in clerks:
        name = c.get("name", c.get("clerk_id", "?"))
        cid = c.get("clerk_id", "?")
        tools = c.get("tools", [])
        tool_names = ", ".join(tools)
        lines.append(f"- **{name}** (ID:{cid}): {len(tools)} 工具: {tool_names}")

    lines.append("\n吏员通过工具调用激活——当你需要执行吏员提供的专业操作时，调用对应的工具。")
    return "\n".join(lines)


def _register_clerk_by_path(shiyi: Shiyi, clerk_dir: str, clerk_id: str):
    """根据目录路径直接注册吏员到引擎（不依赖扫描）"""
    from pathlib import Path
    clerk_path = Path(clerk_dir)
    mcp_script = clerk_path / "mcp_server.py"
    config_path = clerk_path / "clerk.json"
    
    if not mcp_script.exists():
        print(f"MCP脚本不存在: {mcp_script}")
        return False
    if not config_path.exists():
        print(f"配置文件不存在: {config_path}")
        return False
        
    try:
        from shiyi.core.clerk_connector import RemoteClerk
        remote_clerk = RemoteClerk(
            server_script=str(mcp_script),
            config_path=str(config_path),
        )
        shiyi.clerk_registry.register_clerk(remote_clerk)
        print(f"吏员已热注册: {clerk_id}")
        return True
    except Exception as e:
        print(f"吏员热注册失败: {e}")
        return False


@app.post("/api/clerks/create")
async def create_clerk(req: ClerkCreateReq):
    """创建新吏员"""
    from shiyi.core.clerk_creator import ClerkCreator
    creator = ClerkCreator()
    
    # Convert tools format for ClerkCreator
    tools = req.tools if req.tools else None
    
    result = creator.create_non_interactive(
        name=req.name,
        desc=req.description,
        tools=tools,
        requires_llm=req.requires_llm,
        api_keys=req.api_keys if req.api_keys else None,
        model_name=req.model_name,
        provider=req.provider,
        base_url=req.base_url,
    )
    
    if result.get("success"):
        # 热注册到引擎（使用返回的目录路径直接注册，避免扫描延迟）
        try:
            shiyi = get_shiyi()
            clerk_dir = result.get("clerk_dir")
            clerk_id = result.get("clerk_id")
            if clerk_dir and clerk_id:
                _register_clerk_by_path(shiyi, clerk_dir, clerk_id)
        except Exception as e:
            print(f"新吏员热注册失败: {e}")
        return {"status": "ok", "clerk_id": result["clerk_id"], "message": result["message"]}
    else:
        raise HTTPException(400, result.get("error", "创建失败"))

@app.delete("/api/clerks/{clerk_id}")
async def delete_clerk_api(clerk_id: str):
    """删除吏员"""
    # 不允许删除内置吏员
    clerks = _scan_clerks()
    target = None
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            target = c
            break
    if not target:
        raise HTTPException(404, f"吏员 {clerk_id} 未找到")
    if target.get("_source") == "builtin":
        raise HTTPException(403, "内置吏员不允许删除")
    
    from shiyi.core.clerk_creator import delete_clerk
    result = delete_clerk(clerk_id)
    if result.get("success"):
        # 清理用户配置
        try:
            user_cfg_path = _get_clerk_user_config_path(clerk_id)
            if user_cfg_path.exists():
                user_cfg_path.unlink()
        except Exception:
            pass
        # 热注销
        try:
            shiyi = get_shiyi()
            if hasattr(shiyi, 'clerk_registry'):
                shiyi.clerk_registry.unregister_clerk(clerk_id)
        except Exception:
            pass
        return {"status": "ok", "message": result["message"]}
    else:
        raise HTTPException(400, result.get("error", "删除失败"))

class ClerkRenameReq(BaseModel):
    name: str

@app.post("/api/clerks/{clerk_id}/rename")
async def rename_clerk(clerk_id: str, req: ClerkRenameReq):
    """重命名吏员"""
    clerks = _scan_clerks()
    target = None
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            target = c
            break
    if not target:
        raise HTTPException(404, f"吏员 {clerk_id} 未找到")
    if target.get("_source") == "builtin":
        raise HTTPException(403, "内置吏员不允许更名")
    
    new_name = req.name.strip()
    if not new_name:
        raise HTTPException(400, "名称不能为空")
    
    # 修改 clerk.json 中的 name
    clerk_path = Path(target["_path"])
    cj_path = clerk_path / "clerk.json"
    try:
        with open(cj_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        config["name"] = new_name
        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        # 热更新运行中的 worker config
        try:
            shiyi = get_shiyi()
            if hasattr(shiyi, 'clerk_registry') and hasattr(shiyi.clerk_registry, '_clerks'):
                cdata = shiyi.clerk_registry._clerks.get(clerk_id)
                if cdata and cdata.get("worker"):
                    worker = cdata["worker"]
                    if hasattr(worker, 'config') and hasattr(worker.config, 'name'):
                        worker.config.name = new_name
        except Exception:
            pass
        return {"status": "ok", "message": f"吏员已更名为: {new_name}"}
    except Exception as e:
        raise HTTPException(500, f"更名失败: {str(e)}")

class ClerkConfigureReq(BaseModel):
    description: str = ""
    model_name: str = ""
    tools: list = []
    api_keys: list = []
    requires_llm: bool = False

@app.post("/api/clerks/{clerk_id}/configure")
async def configure_clerk(clerk_id: str, req: ClerkConfigureReq):
    """修改吏员配置（描述、模型、工具、API keys 等）"""
    clerks = _scan_clerks()
    target = None
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            target = c
            break
    if not target:
        raise HTTPException(404, f"吏员 {clerk_id} 未找到")
    if target.get("_source") == "builtin":
        raise HTTPException(403, "内置吏员不允许修改配置")
    
    clerk_path = Path(target["_path"])
    cj_path = clerk_path / "clerk.json"
    try:
        with open(cj_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        
        if req.description:
            config["description"] = req.description
        config["model_name"] = req.model_name
        if req.tools:
            config["tools"] = req.tools
            config["capabilities"] = [t["name"] for t in req.tools]
        if req.api_keys:
            config["api_keys"] = req.api_keys
        config["requires_llm"] = req.requires_llm
        
        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        # 重新扫描以刷新运行中的状态
        return {"status": "ok", "message": f"吏员 {clerk_id} 配置已更新"}
    except Exception as e:
        raise HTTPException(500, f"配置修改失败: {str(e)}")

class ClerkSkillAssignReq(BaseModel):
    skills: list  # ["skill_id1", "skill_id2", ...]

@app.post("/api/clerks/{clerk_id}/skills")
async def assign_clerk_skills(clerk_id: str, req: ClerkSkillAssignReq):
    """分配技能到吏员"""
    clerks = _scan_clerks()
    target = None
    for c in clerks:
        if c.get("clerk_id") == clerk_id:
            target = c
            break
    if not target:
        raise HTTPException(404, f"吏员 {clerk_id} 未找到")
    
    # 更新 clerk.json 的 skills 字段
    clerk_path = Path(target["_path"])
    cj_path = clerk_path / "clerk.json"
    try:
        with open(cj_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        config["skills"] = req.skills
        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return {"status": "ok", "message": f"技能已更新", "skills": req.skills}
    except Exception as e:
        raise HTTPException(500, f"技能分配失败: {str(e)}")


# ═══════════════════════════════════════════
# 技能管理 API (v0.19.0)
# ═══════════════════════════════════════════

@app.get("/api/skills")
async def list_skills():
    """列出所有已安装的Skills"""
    shiyi = get_shiyi()
    skills = []
    try:
        skill_loader = shiyi._skill_loader
        if skill_loader:
            for skill_id, sinfo in skill_loader._skills.items():
                skills.append({
                    "id": skill_id,
                    "name": sinfo.name if hasattr(sinfo, 'name') else skill_id,
                    "description": sinfo.description if hasattr(sinfo, 'description') else "",
                    "category": sinfo.category if hasattr(sinfo, 'category') else "",
                    "path": sinfo.path if hasattr(sinfo, 'path') else "",
                    "triggers": sinfo.triggers if hasattr(sinfo, 'triggers') else [],
                    "keywords": sinfo.keywords if hasattr(sinfo, 'keywords') else [],
                })
    except Exception as e:
        return {"skills": [], "error": str(e)}
    return {"skills": skills}

@app.get("/api/skills/{skill_id:path}")
async def get_skill_detail(skill_id: str):
    """获取Skill详情"""
    shiyi = get_shiyi()
    try:
        skill_loader = shiyi._skill_loader
        if skill_loader and skill_id in skill_loader._skills:
            sinfo = skill_loader._skills[skill_id]
            return {
                "id": skill_id,
                "name": sinfo.name if hasattr(sinfo, 'name') else skill_id,
                "description": sinfo.description if hasattr(sinfo, 'description') else "",
                "category": sinfo.category if hasattr(sinfo, 'category') else "",
                "path": sinfo.path if hasattr(sinfo, 'path') else "",
                "triggers": sinfo.triggers if hasattr(sinfo, 'triggers') else [],
                "keywords": sinfo.keywords if hasattr(sinfo, 'keywords') else [],
                "raw_body": sinfo.raw_body if hasattr(sinfo, 'raw_body') else "",
            }
    except Exception as e:
        raise HTTPException(404, f"Skill {skill_id} 未找到: {str(e)}")
    raise HTTPException(404, f"Skill {skill_id} 未找到")

# ═══════════════════════════════════════════
# 路由可视化 API
# ═══════════════════════════════════════════

@app.get("/api/routing/overview")
async def get_routing_overview():
    """获取路由概览：所有Skills + 吏员Skill映射"""
    try:
        shiyi = get_shiyi()
        skill_loader = shiyi._skill_loader
        
        # 所有 Skills
        skills = []
        for skill_id, sinfo in skill_loader._skills.items():
            skills.append({
                "id": skill_id,
                "name": sinfo.name if hasattr(sinfo, 'name') else skill_id,
                "description": sinfo.description if hasattr(sinfo, 'description') else "",
                "category": sinfo.category if hasattr(sinfo, 'category') else "",
                "triggers": sinfo.triggers if hasattr(sinfo, 'triggers') else [],
            })
        
        # 吏员-Skill映射（从文件系统扫描，不走运行中registry）
        clerks_info = _scan_clerks()
        clerk_skill_map = {}
        clerks = []
        for c in clerks_info:
            clerk_skill_map[c.get("clerk_id")] = c.get("skills", [])
            clerks.append({
                "clerk_id": c.get("clerk_id"),
                "name": c.get("name", ""),
                "skills": c.get("skills", []),
                "enabled": c.get("enabled", True),
            })
        
        return {
            "skills": skills,
            "clerks": clerks,
            "clerk_skill_map": clerk_skill_map,
            "total_skills": len(skills),
            "total_clerks": len(clerks),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/routing/prompt")
async def get_routing_prompt():
    """获取完整的路由Prompt，供调试查看"""
    try:
        shiyi = get_shiyi()
        # 从文件系统扫描吏员 + SkillRegistry 构建路由 prompt
        clerks_info = _scan_clerks()
        prompt = shiyi.skill_registry.build_skill_clerk_prompt(clerks_info)
        return {"prompt": prompt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/routing/test")
async def test_routing(req: dict):
    """测试路由决策"""
    try:
        shiyi = get_shiyi()
        query = req.get("query", "")
        if not query:
            raise HTTPException(status_code=400, detail="query 不能为空")
        
        # 调用路由逻辑
        result = shiyi.skill_route_and_dispatch(query)
        return {
            "query": query,
            "skill_id": result.get("skill_id"),
            "clerk_id": result.get("clerk_id"),
            "task_description": result.get("task_description", ""),
            "raw_result": result,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════
# 技能市场 API
# ═══════════════════════════════════════════

@app.get("/api/skills/available")
async def search_available_skills(query: str = ""):
    """搜索 Hermes catalog 可用技能"""
    try:
        # 简单实现：扫描本地技能目录，标记已安装/未安装
        # 后续可以对接 Hermes skills API
        import os
        skills_dir = Path.home() / ".shiyi" / "skills"
        
        # 本地已安装的 skill
        installed = set()
        if skills_dir.exists():
            for cat_dir in skills_dir.iterdir():
                if cat_dir.is_dir():
                    for skill_dir in cat_dir.iterdir():
                        if skill_dir.is_dir():
                            installed.add(f"{cat_dir.name}/{skill_dir.name}")
        
        # 返回可用 skill 列表
        return {
            "installed": list(installed),
            "available": [],  # 预留，后续对接 catalog
            "search_query": query,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/skills/install")
async def install_skill(req: dict):
    """安装技能"""
    try:
        skill_id = req.get("skill_id", "")
        if not skill_id:
            raise HTTPException(status_code=400, detail="skill_id 不能为空")
        
        # 这里实现 skill 安装逻辑
        # 后续可以从 Hermes catalog 下载
        return {"status": "ok", "skill_id": skill_id, "message": "安装成功（占位）"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════
# 配置 API
# ═══════════════════════════════════════════

@app.get("/api/config")
async def get_config():
    main_key_set = bool(os.environ.get("DEEPSEEK_API_KEY"))
    emb_key_set = bool(os.environ.get("EMBEDDING_API_KEY"))
    if main_key_set and emb_key_set:
        config_status = "full"
    elif main_key_set or emb_key_set:
        config_status = "partial"
    else:
        config_status = "none"

    return {
        "main": {
            "key_set": main_key_set,
            "model": os.environ.get("SHIYI_MAIN_LLM_MODEL", DEFAULT_MAIN_LLM_MODEL),
            "base_url": os.environ.get("SHIYI_MAIN_API_BASE", DEFAULT_LLM_BASE_URL),
        },
        "embedding": {
            "key_set": emb_key_set,
            "model": os.environ.get("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            "base_url": os.environ.get("EMBEDDING_API_BASE", DEFAULT_EMBEDDING_BASE_URL),
        },
        "light": {
            "key_set": bool(os.environ.get("SHIYI_LIGHT_API_KEY")),
            "model": os.environ.get("SHIYI_LIGHT_LLM_MODEL", DEFAULT_LIGHT_LLM_MODEL),
            "base_url": os.environ.get("SHIYI_LIGHT_API_BASE", ""),
        },
        "fallback": {
            "key_set": bool(os.environ.get("SHIYI_FALLBACK_API_KEY")),
            "model": os.environ.get("SHIYI_FALLBACK_MODEL", ""),
            "base_url": os.environ.get("SHIYI_FALLBACK_API_BASE", ""),
        },
        "status": config_status,
        "version": __version__,
    }

@app.post("/api/config")
async def save_config(req: ConfigRequest):
    if _IS_FROZEN:
        env_path = Path(sys.executable).parent.parent / ".env"
    else:
        env_path = Path(__file__).parent.parent.parent.parent / ".env"

    MANAGED_KEYS = {
        "DEEPSEEK_API_KEY", "SHIYI_MAIN_LLM_MODEL", "SHIYI_MAIN_API_BASE",
        "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_API_BASE",
        "SHIYI_LIGHT_LLM_MODEL", "SHIYI_LIGHT_API_KEY", "SHIYI_LIGHT_API_BASE",
        "SHIYI_FALLBACK_MODEL", "SHIYI_FALLBACK_API_KEY", "SHIYI_FALLBACK_API_BASE",
    }

    lines = []
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    lines.append(line.rstrip("\n"))
                    continue
                if "=" in stripped:
                    key = stripped.split("=")[0].strip()
                    if key not in MANAGED_KEYS:
                        lines.append(line.rstrip("\n"))

    def add(key: str, value: str):
        if value:
            lines.append(f"{key}={value}")
            os.environ[key] = value

    add("DEEPSEEK_API_KEY", req.main_key)
    add("SHIYI_MAIN_LLM_MODEL", req.main_model)
    add("SHIYI_MAIN_API_BASE", req.main_base_url)
    add("EMBEDDING_API_KEY", req.embedding_key)
    add("EMBEDDING_MODEL", req.embedding_model)
    add("EMBEDDING_API_BASE", req.embedding_base_url)
    add("SHIYI_LIGHT_LLM_MODEL", req.light_model)
    if req.light_key:
        add("SHIYI_LIGHT_API_KEY", req.light_key)
    if req.light_base_url:
        add("SHIYI_LIGHT_API_BASE", req.light_base_url)
    if req.fallback_model:
        add("SHIYI_FALLBACK_MODEL", req.fallback_model)
    if req.fallback_key:
        add("SHIYI_FALLBACK_API_KEY", req.fallback_key)
    if req.fallback_base_url:
        add("SHIYI_FALLBACK_API_BASE", req.fallback_base_url)

    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    global _shiyi
    _shiyi = None

    return {"status": "ok", "message": "配置已保存，引擎已重载"}


# ═══════════════════════════════════════════
# 渠道连接 API — 飞书/微信等渠道配置
# ═══════════════════════════════════════════

_GATEWAY_YAML = Path.home() / ".shiyi" / "gateway.yaml"

# Channel definitions: fields, labels, secret fields (masked in GET)
CHANNEL_DEFS = {
    "feishu": {
        "name": "飞书",
        "icon": "🐦",
        "fields": [
            {"key": "app_id", "label": "App ID", "secret": False, "placeholder": "cli_xxxxxxxxxx"},
            {"key": "app_secret", "label": "App Secret", "secret": True, "placeholder": "xxxxxxxxxxxxxxxx"},
        ],
        "scopes": ["im:message", "im:message:send_as_bot", "im:message:readonly", "im:message.p2p_msg:readonly", "im:message.group_at_msg:readonly", "im:message.group_at_msg.include_bot:readonly", "im:resource", "im:chat", "im:chat:readonly", "im:chat:member"],
        "event": "im.message.receive_v1 (长连接模式)",
        "import_json": '{"scopes":{"tenant":["im:message","im:message:send_as_bot","im:message:readonly","im:message.p2p_msg:readonly","im:message.group_at_msg:readonly","im:message.group_at_msg.include_bot:readonly","im:resource","im:chat","im:chat:readonly","im:chat:member"]}}',
        "guide_link": "",
        "guide_steps": [
            {"step": 1, "title": "创建飞书应用", "desc": "1) 浏览器打开 open.feishu.cn 并用飞书账号登录\n2) 点击「开发者后台」→「创建企业自建应用」\n3) 填写应用名称（如：史佚）和描述（如：AI 记忆助手），上传图标（可选）\n4) 点击「创建」"},
            {"step": 2, "title": "启用机器人能力", "desc": "1) 左侧菜单「应用功能」→「机器人」\n2) 开启「启用机器人」开关\n3) 填写机器人名称和描述\n4) 点击「保存」"},
            {"step": 3, "title": "获取应用凭证", "desc": "1) 左侧菜单「凭证与基础信息」\n2) 复制 App ID（格式 cli_xxx）\n3) 复制 App Secret\n4) 将这两个值填入上方的表单中\n⚠️ App Secret 务必保密"},
            {"step": 4, "title": "批量导入权限（关键步骤）", "desc": "1) 左侧菜单「开发配置」→「权限管理」\n2) 点击「批量导入/导出权限」按钮\n3) 选择「应用身份权限」页签\n4) 将下方 JSON 粘贴到输入框\n5) 点击「格式化 JSON」\n6) 确认权限清单后点击「申请开通」\n⚠️ 敏感权限需管理员审批"},
            {"step": 5, "title": "配置事件订阅", "desc": "1) 左侧菜单「事件与回调」→「事件订阅」\n2) 订阅方式选择「使用长连接接收事件」（WebSocket 模式，无需公网服务器）\n3) 添加事件：im.message.receive_v1\n4) 点击「保存」"},
            {"step": 6, "title": "发布应用", "desc": "1) 左侧菜单「版本管理与发布」→「创建版本」\n2) 填写版本号（如 1.0.0）和更新说明\n3) 点击「申请线上发布」\n4) 等待管理员审批\n审批通过后即可使用"},
        ],
    },
    "wechat": {
        "name": "微信",
        "icon": "💬",
        "fields": [],
        "scopes": [],
        "event": "",
        "guide_link": "",
    },
}


def _load_gateway_yaml() -> dict:
    """Load gateway.yaml, return raw dict."""
    if not _GATEWAY_YAML.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(_GATEWAY_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_gateway_yaml(data: dict):
    """Save gateway.yaml atomically."""
    import yaml
    _GATEWAY_YAML.parent.mkdir(parents=True, exist_ok=True)
    tmp = _GATEWAY_YAML.with_suffix(".tmp")
    tmp.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(_GATEWAY_YAML)


@app.get("/api/channels")
async def get_channels():
    """Get all channel configs (secrets masked)."""
    gw = _load_gateway_yaml()
    result = {}
    for ch_id, ch_def in CHANNEL_DEFS.items():
        ch_cfg = gw.get(ch_id, {})
        fields = []
        for f in ch_def["fields"]:
            val = ch_cfg.get(f["key"], "")
            fields.append({
                "key": f["key"],
                "label": f["label"],
                "secret": f["secret"],
                "placeholder": f["placeholder"],
                "value_set": bool(val),
                "value": "" if f["secret"] else val,  # never expose secrets
            })
        result[ch_id] = {
            "name": ch_def["name"],
            "icon": ch_def["icon"],
            "fields": fields,
            "scopes": ch_def.get("scopes", []),
            "event": ch_def.get("event", ""),
            "import_json": ch_def.get("import_json", ""),
            "guide_link": ch_def.get("guide_link", ""),
            "guide_steps": ch_def.get("guide_steps", []),
        }
    return result


@app.post("/api/channels")
async def save_channel_config(req: ChannelConfigRequest):
    """Save a channel's config to gateway.yaml."""
    ch_id = req.channel
    if ch_id not in CHANNEL_DEFS:
        raise HTTPException(400, f"Unknown channel: {ch_id}")

    gw = _load_gateway_yaml()
    existing = gw.get(ch_id, {})

    # Merge: only update provided fields, keep existing ones
    for k, v in req.config.items():
        if v:
            existing[k] = v
        elif k in existing and not v:
            # Empty value = remove the key
            del existing[k]

    gw[ch_id] = existing
    _save_gateway_yaml(gw)
    return {"status": "ok", "message": f"{CHANNEL_DEFS[ch_id]['name']}渠道配置已保存"}


@app.get("/api/channels/{channel_id}/test")
async def test_channel_connection(channel_id: str):
    """Test channel connectivity (currently only feishu WS ping)."""
    if channel_id not in CHANNEL_DEFS:
        raise HTTPException(400, f"Unknown channel: {channel_id}")

    gw = _load_gateway_yaml()
    ch_cfg = gw.get(channel_id, {})

    if channel_id == "feishu":
        app_id = ch_cfg.get("app_id", "")
        app_secret = ch_cfg.get("app_secret", "")
        if not app_id or not app_secret:
            return {"status": "error", "message": "请先填写 App ID 和 App Secret"}
        try:
            import urllib.request
            data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
            req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("code") == 0:
                return {"status": "ok", "message": "飞书连接成功 ✅"}
            else:
                return {"status": "error", "message": f"认证失败: {result.get('msg', '未知错误')}"}
        except Exception as e:
            return {"status": "error", "message": f"连接失败: {str(e)}"}

    return {"status": "ok", "message": "测试功能暂未实现"}


# ═══════════════════════════════════════════
# 管家 API — 吏员协同调度看板
# ═══════════════════════════════════════════

class StewardRunRequest(BaseModel):
    request: str
    auto_execute: bool = True


@app.get("/api/steward")
async def steward_status():
    """管家看板总览"""
    s = get_shiyi()
    return s.steward_status()


@app.get("/api/steward/task/{task_id}")
async def steward_task_detail(task_id: str):
    """管家任务详情"""
    s = get_shiyi()
    detail = s.steward_task(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return detail


@app.post("/api/steward/run")
async def steward_run(req: StewardRunRequest):
    """创建并执行管家任务"""
    s = get_shiyi()
    result = s.steward_run(req.request, auto_execute=req.auto_execute)
    if "error" in result and result.get("task_id") is None:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


# ═══════════════════════════════════════════
# 记忆面板 API
# ═══════════════════════════════════════════

@app.get("/api/memories")
async def get_memories(query: str = ""):
    """记忆晶体概览 + 最近记录 / 搜索"""
    s = get_shiyi()
    stats = {"total_fragments": 0, "hot_count": 0, "cold_count": 0}
    recent = []
    try:
        if hasattr(s, '_memory') and s._memory:
            mem = s._memory
            if hasattr(mem, 'stats'):
                raw = mem.stats()
                frag_stats = raw.get("fragments", {})
                layer_stats = frag_stats.get("by_layer", {})
                stats["total_fragments"] = frag_stats.get("total", 0)
                stats["hot_count"] = layer_stats.get("hot", 0)
                stats["cold_count"] = layer_stats.get("cold", 0)
            if hasattr(mem, 'recall'):
                raw = mem.recall("", top_k=10)
                # recall() returns List[Dict]: [{"fragment": Fragment, "score": float, "source": str}, ...]
                if isinstance(raw, list):
                    for item in raw:
                        frag = item.get("fragment", {}) if isinstance(item, dict) else None
                        if frag:
                            recent.append({
                                "content": getattr(frag, "fact_kernel", "") or "",
                                "domain": getattr(frag, "scene_shell", None) and getattr(frag.scene_shell, "domain", "") or "",
                                "created_at": str(getattr(frag, "created_at", "") or ""),
                            })
                        else:
                            recent.append({
                                "content": str(item.get("content", item.get("fact", ""))),
                                "domain": str(item.get("domain", "")),
                                "created_at": str(item.get("created_at", item.get("timestamp", ""))),
                            })
                else:
                    # fallback: old dict-return format
                    recalled = raw.get("recalled", [])
                    for item in recalled:
                        recent.append({
                            "content": item.get("content", item.get("fact", "")),
                            "domain": item.get("domain", ""),
                            "created_at": item.get("created_at", item.get("timestamp", "")),
                        })
    except Exception as e:
        return {"stats": stats, "recent": [], "error": str(e)}

    # If query provided, also search
    if query:
        results = []
        try:
            if hasattr(s, '_memory') and s._memory and hasattr(s._memory, 'recall'):
                raw = s._memory.recall(query, top_k=20)
                if isinstance(raw, list):
                    for item in raw:
                        frag = item.get("fragment", {}) if isinstance(item, dict) else None
                        if frag:
                            results.append({
                                "content": getattr(frag, "fact_kernel", "") or "",
                                "domain": getattr(frag, "scene_shell", None) and getattr(frag.scene_shell, "domain", "") or "",
                                "created_at": str(getattr(frag, "created_at", "") or ""),
                                "score": item.get("score", 0),
                            })
        except Exception:
            pass
        return {"stats": stats, "results": results}

    return {"stats": stats, "recent": recent}


# ═══════════════════════════════════════════
# 文件管理
# ═══════════════════════════════════════════

_WORKSPACE = Path.home() / ".shiyi" / "workspace"


@app.get("/api/files")
async def list_files():
    """列出工作区所有文件"""
    files = []
    if _WORKSPACE.exists():
        for f in sorted(_WORKSPACE.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
    return {"files": files}


@app.get("/api/files/download")
async def download_file(name: str = ""):
    """下载工作区文件"""
    if not name:
        return {"error": "缺少文件名参数"}
    filepath = (_WORKSPACE / name).resolve()
    # 安全检查：必须在 workspace 内
    if not str(filepath).startswith(str(_WORKSPACE.resolve())):
        return {"error": "不允许的路径"}, 403
    if not filepath.exists():
        return {"error": "文件不存在"}, 404
    return FileResponse(str(filepath), filename=name, media_type="application/octet-stream")


# ═══════════════════════════════════════════
# 静态页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    # 始终返回 index.html，不再根据API key判断跳setup.html
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>index.html 未找到</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

HOST = "0.0.0.0"
PORT = 8520

def _auto_start_gateways(shiyi_instance):
    """Auto-start gateways for configured channels in background threads."""
    import threading
    gw_yaml = Path.home() / ".shiyi" / "gateway.yaml"
    if not gw_yaml.exists():
        return
    try:
        import yaml
        config = yaml.safe_load(gw_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return

    # Check feishu
    feishu_cfg = config.get("feishu", {})
    if feishu_cfg.get("app_id") and feishu_cfg.get("app_secret"):
        print("检测到飞书配置，自动启动飞书网关...")
        try:
            from shiyi.shell.gateway.run import run as gateway_run
            t = threading.Thread(target=gateway_run, args=("feishu", shiyi_instance),
                                 daemon=True, name="gateway-feishu")
            t.start()
            print("飞书网关已启动（后台线程）")
        except Exception as e:
            print(f"飞书网关启动失败: {e}")


def main():
    import socket
    print(f"史佚 Web Chat UI v{__version__} — Developed by LiGuo LeGang")
    print(f"本地访问: http://localhost:{PORT}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        print(f"局域网访问: http://{ip}:{PORT}")
    except Exception:
        pass
    print(f"按 Ctrl+C 停止")

    try:
        shiyi = get_shiyi()
        status = "真实LLM" if shiyi.llm_available else "Mock模式"
        print(f"引擎就绪 ({status})")
        
        # 自动注册所有已启用的吏员到引擎
        try:
            clerks = _scan_clerks()
            registered_count = 0
            for c in clerks:
                if c.get("enabled") is not False:
                    clerk_id = c.get("clerk_id")
                    if clerk_id and c.get("_path"):
                        _register_clerk_by_path(shiyi, c["_path"], clerk_id)
                        registered_count += 1
            if registered_count > 0:
                print(f"已自动注册 {registered_count} 个吏员")
        except Exception as e:
            print(f"吏员自动注册警告: {e}")
    except Exception as e:
        print(f"引擎初始化警告: {e}")

    # Auto-start configured gateways (daemon threads, auto-stop on exit)
    try:
        _auto_start_gateways(shiyi)
    except NameError:
        pass  # shiyi not initialized

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

@app.delete("/api/files")
async def delete_file(name: str = ""):
    """删除工作区文件"""
    if not name:
        raise HTTPException(status_code=400, detail="缺少文件名参数")
    safe_name = Path(name).name
    filepath = (_WORKSPACE / safe_name).resolve()
    # 安全检查：必须在 workspace 内
    if not str(filepath).startswith(str(_WORKSPACE.resolve())):
        raise HTTPException(status_code=403, detail="不允许的路径")
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    filepath.unlink()
    return {"status": "ok", "message": f"已删除 {safe_name}"}


# ═══════════════════════════════════════════
# 管家 Steward API (Phase 2)
# ═══════════════════════════════════════════

@app.get("/api/steward/monitor")
async def steward_monitor_status():
    """获取监控状态"""
    try:
        return _shiyi.steward_monitor_status()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/steward/alerts")
async def steward_alerts(clerk_id: str = ""):
    """获取监控告警"""
    try:
        return {"alerts": _shiyi.steward_clerk_alerts(clerk_id or None)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/steward/monitor/start")
async def steward_start_monitor():
    """启动后台上校监控"""
    try:
        _shiyi.steward_start_monitor()
        return {"status": "ok", "message": "监控已启动"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/steward/monitor/stop")
async def steward_stop_monitor():
    """停止后台监控"""
    try:
        _shiyi.steward_stop_monitor()
        return {"status": "ok", "message": "监控已停止"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════
# 流水线 Pipeline API (Phase 3)
# ═══════════════════════════════════════════

@app.post("/api/steward/pipelines")
async def steward_create_pipeline(body: dict):
    """创建流水线"""
    try:
        name = body.get("name", "未命名流水线")
        stages_def = body.get("stages", [])
        if not stages_def:
            raise HTTPException(status_code=400, detail="stages 不能为空")
        result = _shiyi.steward_create_pipeline(name, stages_def)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/steward/pipelines/{pipeline_id}/execute")
async def steward_execute_pipeline(pipeline_id: str):
    """执行流水线"""
    try:
        result = _shiyi.steward_execute_pipeline(pipeline_id)
        if not result.get("success", True) and result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/steward/pipelines/{pipeline_id}")
async def steward_get_pipeline(pipeline_id: str):
    """获取流水线状态"""
    try:
        result = _shiyi.steward_get_pipeline(pipeline_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"流水线不存在: {pipeline_id}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/steward/pipelines")
async def steward_list_pipelines():
    """列出所有流水线"""
    try:
        return {"pipelines": _shiyi.steward_list_pipelines()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    main()
