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
    from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
    import uvicorn
except ImportError:
    print("需要安装依赖: pip install fastapi uvicorn python-multipart")
    sys.exit(1)

from shiyi.engine import Shiyi
from shiyi.shell.llm_caller import create_llm_caller
from shiyi.shell.tools import ToolRegistry, web_search_tool, file_read_tool, file_write_tool
from shiyi.shell.embedding_caller import create_embedding_caller
from shiyi.shell import __version__

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
    main_model: str = "deepseek-v4-pro"
    main_base_url: str = "https://api.deepseek.com/v1"
    embedding_model: str = "BAAI/bge-m3"
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    light_model: str = "deepseek-v4-flash"
    light_key: str = ""
    light_base_url: str = ""
    fallback_model: str = ""
    fallback_key: str = ""
    fallback_base_url: str = ""

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
    """解析 session_id：空则返回默认"""
    return session_id.strip() if session_id and session_id.strip() else _get_default_session_id()

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
                "⚠️ API 尚未就绪。\n"
                "请先配置主模型 API Key 和 Embedding API Key，再开始对话。\n"
                "点击右上角齿轮进入设置。"
            )
            status = "unconfigured"
        else:
            reply_text = await asyncio.to_thread(shiyi.chat, req.message)
            status = "ok"
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


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话接口 — SSE (Server-Sent Events)
    
    相比 /api/chat 阻塞等待完整回复，此端点逐 token 推送，
    用户无需等待 30-60 秒即可看到回复逐字出现。
    不支持 Function Calling / Tool Calling。
    """
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

    async def generate():
        shiyi = get_shiyi()
        if not shiyi.llm_available:
            yield f"data: {json.dumps({'token': '⚠️ API 尚未就绪。请先配置主模型 API Key 和 Embedding API Key，再开始对话。点击右上角齿轮进入设置。'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        full_reply = ""
        try:
            for token in shiyi.chat_stream(req.message):
                full_reply += token
                yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # 流完成后保存回复到数据库
        if full_reply:
            conn2 = _get_db()
            try:
                reply_msg_id = str(uuid.uuid4())
                conn2.execute(
                    "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                    (reply_msg_id, session_id, "shiyi", full_reply, datetime.datetime.utcnow().isoformat())
                )
                conn2.execute("UPDATE sessions SET updated_at=? WHERE id=?", (datetime.datetime.utcnow().isoformat(), session_id))
                conn2.commit()
            finally:
                conn2.close()

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

# ═══ 并发对话：非阻塞入队 + 轮询取回复 ═══

class EnqueueRequest(BaseModel):
    message: str
    session_id: str = ""

class EnqueueResponse(BaseModel):
    msg_id: str
    status: str = "queued"

@app.post("/api/chat/enqueue", response_model=EnqueueResponse)
async def chat_enqueue(req: EnqueueRequest):
    """非阻塞入队 — 立即返回 msg_id，后台处理"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = _resolve_session(req.session_id)
    now = datetime.datetime.utcnow().isoformat()

    # 保存用户消息到 DB
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

    # 入队到引擎后台处理
    shiyi = get_shiyi()
    msg_id = await asyncio.to_thread(
        shiyi.enqueue_message, req.message, user_msg_id, session_id
    )

    return EnqueueResponse(msg_id=msg_id)


@app.get("/api/chat/replies")
async def chat_replies(since_msg_id: str = ""):
    """轮询已完成的回复"""
    shiyi = get_shiyi()
    replies = await asyncio.to_thread(shiyi.get_pending_replies, since_msg_id)

    # 保存回复到数据库
    for entry in replies:
        reply_text = entry.get("reply", "")
        if not reply_text and not entry.get("error"):
            continue
        if entry.get("error"):
            reply_text = f"错误: {entry['error']}"

        batch_ids = entry.get("batch_ids", [entry["msg_id"]])
        session_id = _resolve_session("")  # 从第一个用户消息反查 session
        conn = _get_db()
        try:
            cur = conn.execute(
                "SELECT session_id FROM messages WHERE id=?",
                (entry["msg_id"],)
            )
            row = cur.fetchone()
            if row:
                session_id = row["session_id"]
        finally:
            conn.close()

        now = datetime.datetime.utcnow().isoformat()
        conn2 = _get_db()
        try:
            reply_msg_id = str(uuid.uuid4())
            conn2.execute(
                "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (reply_msg_id, session_id, "shiyi", reply_text, now)
            )
            conn2.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            conn2.commit()
        finally:
            conn2.close()

        # 按 batch_ids 返回，前端据此渲染
        entry["_saved_as"] = reply_msg_id
        entry["session_id"] = session_id

    return {"replies": replies}


# ═══════════════════════════════════════════
# 健康检查 / 配置 / 异步 / 看板 (保持原有)
# ═══════════════════════════════════════════

@app.get("/api/health")
async def health():
    shiyi = get_shiyi()
    return {
        "status": "ok",
        "llm_available": shiyi.llm_available,
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
        result_str = await asyncio.to_thread(shiyi.chat_async, req.message)
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

    # 1. 内置吏员（项目 clerk-* 目录）
    project_clerks = Path(__file__).parent
    for entry in sorted(project_clerks.iterdir()):
        if entry.is_dir() and entry.name.startswith("clerk-"):
            info = _read_clerk_info(entry)
            if info and info["clerk_id"] not in seen_ids:
                info["_source"] = "builtin"
                info["_path"] = str(entry)
                _merge_user_config(info)
                results.append(info)
                seen_ids.add(info["clerk_id"])

    # 2. 用户安装的吏员
    if USER_CLERKS_DIR.exists():
        for entry in sorted(USER_CLERKS_DIR.iterdir()):
            if entry.is_dir() and entry.name.startswith("clerk-"):
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
    for key_name in declared_keys:
        clerk_info["_key_status"][key_name] = bool(saved_keys.get(key_name))

class ClerkConfigSave(BaseModel):
    api_keys: dict = {}  # {KEY_NAME: "sk-xxx"}

@app.get("/api/clerks")
async def list_clerks():
    """列出所有已安装的吏员"""
    return {"clerks": _scan_clerks()}

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
    """保存吏员 API key 配置"""
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

    declared_keys = found.get("api_keys", [])
    for key_name, key_value in req.api_keys.items():
        if key_name in declared_keys and key_value.strip():
            user_cfg["api_keys"][key_name] = key_value.strip()

    _write_user_config(clerk_id, user_cfg)
    return {"status": "ok", "message": f"吏员 {clerk_id} 的 API key 已保存"}

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
            "model": os.environ.get("SHIYI_MAIN_LLM_MODEL", "deepseek-v4-pro"),
            "base_url": os.environ.get("SHIYI_MAIN_API_BASE", "https://api.deepseek.com/v1"),
        },
        "embedding": {
            "key_set": emb_key_set,
            "model": os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3"),
            "base_url": os.environ.get("EMBEDDING_API_BASE", "https://api.siliconflow.cn/v1"),
        },
        "light": {
            "key_set": bool(os.environ.get("SHIYI_LIGHT_API_KEY")),
            "model": os.environ.get("SHIYI_LIGHT_LLM_MODEL", "deepseek-v4-flash"),
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
# 静态页面
# ═══════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    if not os.environ.get("DEEPSEEK_API_KEY") or not os.environ.get("EMBEDDING_API_KEY"):
        setup_path = STATIC_DIR / "setup.html"
        if setup_path.exists():
            return HTMLResponse(setup_path.read_text(encoding="utf-8"))

    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>index.html 未找到</h1>", status_code=404)
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

HOST = "0.0.0.0"
PORT = 8520

def main():
    print(f"史佚 Web Chat UI v{__version__} — Developed by LiGuo LeGang")
    print(f"打开浏览器访问: http://localhost:{PORT}")
    print(f"按 Ctrl+C 停止")

    try:
        shiyi = get_shiyi()
        status = "真实LLM" if shiyi.llm_available else "Mock模式"
        print(f"引擎就绪 ({status})")
    except Exception as e:
        print(f"引擎初始化警告: {e}")

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")

if __name__ == "__main__":
    main()
