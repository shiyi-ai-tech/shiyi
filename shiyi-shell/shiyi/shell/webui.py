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
import re
import shutil
import uuid
import sqlite3
import datetime
import json
from pathlib import Path
from typing import Optional

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
    DEFAULT_LLM_PROVIDER,
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
SANDBOX_DIR = Path.home() / ".shiyi"  # clerk-default 实际沙箱根目录
# 沙箱中排除的敏感子目录（不提供下载）
_SANDBOX_EXCLUDED_DIRS = {"data", "weixin", "gateway", "skills", "clerks"}
MAX_SESSIONS = 10
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

# ═══ MEDIA: 标签处理（与 gateway/run.py 保持一致） ═══
_MEDIA_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.mov', '.avi',
               '.mkv', '.webm', '.3gp', '.mp3', '.ogg', '.wav', '.silk', '.amr',
               '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
               '.txt', '.md', '.json', '.yaml', '.yml', '.toml', '.csv', '.xml',
               '.html', '.htm', '.py', '.js', '.ts', '.zip', '.tar', '.gz',
               '.tar.gz', '.bz2', '.7z', '.log'}

_MEDIA_RE = re.compile(
    r'[`"\']?MEDIA:\s*((?:[A-Za-z]:[/\\\\]|/|~/)\S+\.(?:' +
    '|'.join(ext.lstrip('.') for ext in _MEDIA_EXTS) +
    r'))\b',
    re.IGNORECASE
)

def _process_media_in_reply(reply_text: str) -> tuple:
    """Parse MEDIA: tags from LLM reply, copy files into WORKSPACE_DIR.

    Returns (cleaned_reply, media_files) where media_files is list of {name, size}.
    Files that don't exist are silently skipped (logged via logger).
    """
    if not reply_text or 'MEDIA:' not in reply_text:
        return reply_text, []

    import logging
    logger = logging.getLogger("webui.media")
    paths_seen = set()
    media_files = []

    for match in _MEDIA_RE.finditer(reply_text):
        path = match.group(1).strip()
        # Strip surrounding quotes/backticks
        if len(path) >= 2 and path[0] == path[-1] and path[0] in '`"\'\`':
            path = path[1:-1]
        path = path.lstrip('`"\'').rstrip('`"\',.;:)}\\\\\'')
        path = os.path.expanduser(path)

        if not path or path in paths_seen:
            continue
        if not os.path.isfile(path):
            logger.warning("MEDIA: file not found: %s", path)
            continue

        paths_seen.add(path)
        try:
            dest = WORKSPACE_DIR / os.path.basename(path)
            shutil.copy2(path, dest)
            media_files.append({
                "name": dest.name,
                "size": dest.stat().st_size,
            })
            logger.info("MEDIA: copied to workspace: %s -> %s", path, dest)
        except Exception:
            logger.exception("MEDIA: copy failed: %s", path)

    # Strip MEDIA: tags from reply text so raw paths don't show in chat
    cleaned = _MEDIA_RE.sub('', reply_text).strip()
    return cleaned, media_files

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

# ═══ 翻译辅助函数（零LLM调用，不阻塞事件循环）═══

def _contains_chinese(text: str) -> bool:
    """检测文本是否包含中文字符"""
    return any('一' <= c <= '鿿' for c in text)

# 中文→英文关键词映射（覆盖常见搜索场景）
_CN_TO_EN = {
    "视频": "video ascii-video manim render",
    "制作": "generate create render manim comfyui",
    "图片": "image pixel art diagram screenshot photo",
    "代码": "code program develop script python",
    "调试": "debug debugpy inspect traceback",
    "音乐": "music audio song spotify audiocraft",
    "游戏": "game minecraft pokemon",
    "搜索": "search web arxiv research lookup",
    "笔记": "note obsidian memory",
    "邮件": "email himalaya gmail",
    "文档": "document pdf ocr markdown",
    "设计": "design diagram sketch architecture draw",
    "部署": "deploy docker server devops",
    "测试": "test testing pytest tdd",
    "安全": "security red-team jailbreak",
    "数据": "data database sql analytics",
    "智能": "agent ai llm model",
    "语音": "voice tts stt speech audio",
    "画图": "diagram draw excalidraw architecture sketch",
    "写作": "write writing plan research-paper",
    "家居": "home smart-home hue",
    "绘画": "draw paint pixel art sketch",
    "ppt": "presentation slide powerpoint",
    "幻灯片": "presentation slide powerpoint",
    "演示": "presentation slide demo",
    "表格": "spreadsheet excel csv",
    "聊天": "chat message conversation",
    "翻译": "translate language i18n",
    "爬虫": "crawl scrape spider web",
    "监控": "monitor observe watch",
    "日程": "calendar schedule plan",
    "提醒": "reminder notification alert",
    "天气": "weather forecast",
    "新闻": "news feed rss",
    "地图": "map location geographic",
    "压缩": "compress zip archive",
    "加密": "encrypt decrypt security",
    "抓包": "network capture proxy",
    "代理": "proxy agent delegate",
    "生成": "generate create render",
    "分析": "analyze analytics data",
    "转换": "convert transform format",
    "下载": "download fetch",
    "上传": "upload push",
    "编程": "code program develop python",
    "学习": "learn study tutorial",
    "自动化": "automate schedule cron task",
    "报告": "report document pdf",
    "报表": "report chart dashboard",
    "图表": "chart diagram graph visualization",
    "流程图": "flowchart diagram excalidraw",
    "脑图": "mindmap brainstorm",
    "思维导图": "mindmap brainstorm",
}

def _chinese_to_english_keywords(query: str) -> str:
    """将中文查询转换为英文关键词（纯映射，零LLM调用）

    策略：对每个中文词，同时保留原词和映射的英文词。
    这样搜 "ppt制作" 会同时搜 "ppt制作" + "ppt" + "presentation" + "制作" + "generate create render"
    """
    words = query.split()
    all_terms = set()
    for word in words:
        all_terms.add(word)  # 原词始终保留
        w_lower = word.lower()
        # 精确匹配
        if w_lower in _CN_TO_EN:
            all_terms.update(_CN_TO_EN[w_lower].split())
        # 子串匹配（"制作" in "ppt制作"）
        for cn, en in _CN_TO_EN.items():
            if cn in word and cn != word:
                all_terms.add(cn)
                all_terms.update(en.split())
    return " ".join(sorted(all_terms))

# 技能描述快速中文翻译（基于领域关键词，零LLM调用）
_DESC_ZH_MAP = {
    "debug": "调试", "debugging": "调试", "python": "Python", "code": "代码",
    "search": "搜索", "web": "网页", "file": "文件", "memory": "记忆",
    "email": "邮件", "document": "文档", "pdf": "PDF", "image": "图片",
    "video": "视频", "audio": "音频", "music": "音乐", "game": "游戏",
    "chart": "图表", "diagram": "图表", "data": "数据", "database": "数据库",
    "deploy": "部署", "docker": "Docker", "test": "测试", "security": "安全",
    "presentation": "演示文稿", "slide": "幻灯片", "spreadsheet": "电子表格",
    "translate": "翻译", "weather": "天气", "calendar": "日历", "reminder": "提醒",
    "notification": "通知", "monitor": "监控", "crawl": "爬虫", "scrape": "抓取",
    "generate": "生成", "analyze": "分析", "convert": "转换", "automate": "自动化",
    "schedule": "定时任务", "report": "报告", "visualization": "可视化",
    "screenshot": "截图", "drawing": "绘图", "sketch": "草图",
    "agent": "智能体", "llm": "大模型", "ai": "AI",
    "voice": "语音", "speech": "语音", "tts": "语音合成",
    "creative": "创意", "productivity": "效率", "communication": "通信",
    "research": "研究", "science": "科学", "education": "教育",
    "finance": "金融", "legal": "法律", "health": "健康",
    "social": "社交", "media": "媒体", "network": "网络",
    "system": "系统", "shell": "Shell", "terminal": "终端",
    "browser": "浏览器", "automation": "自动化", "workflow": "工作流",
}

def _quick_translate_description(desc: str, category: str = "") -> str:
    """基于关键词的快速描述翻译（零LLM，不阻塞）

    策略：提取英文关键词 → 翻译为中文标签，拼接为简短描述。
    如果描述本身就是中文则直接返回。
    """
    if not desc:
        return ""
    # 如果描述已经是中文为主，直接返回
    zh_count = sum(1 for c in desc if '一' <= c <= '鿿')
    if zh_count > len(desc) * 0.3:
        return desc
    parts = []
    seen = set()
    words = desc.split()
    for w in words:
        w_clean = w.lower().strip(".,;:!?)(")
        if w_clean in _DESC_ZH_MAP and _DESC_ZH_MAP[w_clean] not in seen:
            parts.append(_DESC_ZH_MAP[w_clean])
            seen.add(_DESC_ZH_MAP[w_clean])
    if parts:
        # 拼接中文标签 + 保留原文
        return f"{'·'.join(parts[:5])}—{desc[:60]}"
    return ""


def _write_quick_description_zh(skill_id: str):
    """安装后立即将快速翻译的 description_zh 写入 SKILL.md（同步，毫秒级）

    确保前端 loadSkills() 能立即看到中文描述。
    后续 _translate_skill_after_install 会用 LLM 翻译更高质量版本覆盖。
    """
    import re as _re
    skills_dir = Path.home() / ".shiyi" / "skills"
    skill_md = skills_dir / skill_id / "SKILL.md"
    if not skill_md.exists():
        return
    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        return
    fm_match = _re.match(r"^---\s*\n(.*?)\n---\s*\n", content, _re.DOTALL)
    if not fm_match:
        return
    fm_text = fm_match.group(1)
    # 如果已有 description_zh 就不覆盖
    if "description_zh:" in fm_text:
        return
    # 提取 description
    desc = ""
    for line in fm_text.split("\n"):
        if line.strip().startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip("'\"")
            break
    if not desc:
        return
    desc_zh = _quick_translate_description(desc, "")
    if not desc_zh:
        return
    new_fm = fm_text + f'\ndescription_zh: "{desc_zh}"'
    new_content = f"---\n{new_fm}\n---\n{content[fm_match.end():]}"
    try:
        skill_md.write_text(new_content, encoding="utf-8")
    except Exception:
        pass


def _translate_skill_after_install(shiyi: Shiyi, skill_id: str):
    """安装后翻译 skill 的 description 和 body 为中文，写回 SKILL.md

    使用后台线程调用 LLM，避免阻塞 FastAPI 事件循环。
    """
    import threading

    def _do_translate():
        if not shiyi._llm or not shiyi._llm.is_available():
            return
        skills_dir = Path.home() / ".shiyi" / "skills"
        skill_md = skills_dir / skill_id / "SKILL.md"
        if not skill_md.exists():
            return

        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception:
            return

        # 解析 YAML frontmatter
        import re as _re
        fm_match = _re.match(r"^---\s*\n(.*?)\n---\s*\n", content, _re.DOTALL)
        if not fm_match:
            return

        fm_text = fm_match.group(1)
        body = content[fm_match.end():]

        # 检查是否已有翻译 — 不重复
        if "description_zh:" in fm_text and "body_zh:" in fm_text:
            return

        # 提取 description
        desc = ""
        for line in fm_text.split("\n"):
            if line.strip().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("'\"")
                break

        # 翻译（同步调用在线程中不阻塞事件循环）
        desc_zh = ""
        body_zh = ""
        try:
            if desc and "description_zh:" not in fm_text:
                result = shiyi._llm.chat(
                    messages=[{"role": "user", "content": f"Translate this skill description to Chinese (only return the translation, keep it concise): {desc}"}],
                    temperature=0.1, max_tokens=200,
                )
                desc_zh = str(result).strip()
            if body.strip() and "body_zh:" not in fm_text:
                body_to_translate = body[:3000]
                result = shiyi._llm.chat(
                    messages=[{"role": "user", "content": f"Translate the following skill content to Chinese. Keep markdown formatting. Only return the translation:\n\n{body_to_translate}"}],
                    temperature=0.1, max_tokens=1000,
                )
                body_zh = str(result).strip()
        except Exception as e:
            print(f"[webui] Background translation failed: {e}")
            return

        # 写回 SKILL.md
        new_fm_lines = fm_text.split("\n")
        if desc_zh:
            new_fm_lines.append(f'description_zh: "{desc_zh}"')
        if body_zh:
            new_fm_lines.append('body_zh: |')
            for bl in body_zh.split("\n"):
                new_fm_lines.append(f'  {bl}')

        new_fm = "\n".join(new_fm_lines)
        new_content = f"---\n{new_fm}\n---\n{body}"
        try:
            skill_md.write_text(new_content, encoding="utf-8")
        except Exception as e:
            print(f"[webui] Failed to write translation to SKILL.md: {e}")

    # 在后台线程中执行，不阻塞事件循环
    t = threading.Thread(target=_do_translate, daemon=True)
    t.start()


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
                # 将 clerk-default 的工具提升为全局共享
                promoted = _shiyi.clerk_registry.promote_clerk_tools_to_global(remote_clerk.config.clerk_id)
                print(f"远程吏员已注册: {remote_clerk.config.clerk_id}, {promoted} 工具已提升为全局")
            else:
                import sys
                sys.path.insert(0, str(clerk_path))
                from worker import ClerkWorker
                local_clerk = ClerkWorker(str(clerk_path / "clerk.json"))
                _shiyi.clerk_registry.register_clerk(local_clerk)
                # 将 clerk-default 的工具提升为全局共享
                promoted = _shiyi.clerk_registry.promote_clerk_tools_to_global(local_clerk.config.clerk_id)
                print(f"本地吏员已注册: {local_clerk.config.clerk_id}, {promoted} 工具已提升为全局")
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
    files: list = []  # workspace files written during this turn

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

def _resolve_safe_download_path(path: str) -> Optional[Path]:
    """解析安全的下载路径：在 WORKSPACE_DIR 或 SANDBOX_DIR 内查找，排除敏感目录"""
    if not path:
        return None
    safe_name = Path(path).name

    # 1. 先在 WORKSPACE_DIR 中查找
    for base_dir in [WORKSPACE_DIR, SANDBOX_DIR]:
        file_path = (base_dir / safe_name).resolve()
        base_resolved = base_dir.resolve()
        if not str(file_path).startswith(str(base_resolved)):
            continue
        # 排除敏感子目录
        rel = file_path.relative_to(base_resolved)
        if any(part in _SANDBOX_EXCLUDED_DIRS for part in rel.parts):
            continue
        if file_path.exists() and file_path.is_file():
            return file_path
    return None


@app.get("/api/download")
async def download_file(path: str = ""):
    """下载 workspace 中的文件"""
    file_path = _resolve_safe_download_path(path)
    if file_path is None:
        raise HTTPException(status_code=404, detail="文件不存在或路径不允许")
    return FileResponse(str(file_path), filename=file_path.name)

@app.get("/api/workspace-files")
async def list_workspace_files():
    """列出 workspace 中的文件（包括 WORKSPACE_DIR 和 SANDBOX_DIR 下的普通文件）"""
    files = []
    seen_names = set()
    for base_dir in [WORKSPACE_DIR, SANDBOX_DIR]:
        try:
            for f in sorted(base_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if f.is_file() and f.name not in seen_names:
                    # 排除敏感文件（配置、凭据等）
                    if f.suffix in {'.json', '.yaml', '.yml', '.env', '.db'} and base_dir == SANDBOX_DIR:
                        continue
                    seen_names.add(f.name)
                    files.append({"name": f.name, "size": f.stat().st_size})
        except Exception:
            pass
    return {"files": files}

def _scan_new_workspace_files(since: float) -> list:
    """Scan workspace for files modified since `since` (unix timestamp).
    Returns list of {name, size} for readable files (.txt/.md/.json/.csv/.log/.html/.py).
    """
    import time
    _READABLE_EXTS = {'.txt', '.md', '.json', '.csv', '.log', '.html', '.py', '.js', '.css'}
    files = []
    for base_dir in [WORKSPACE_DIR, SANDBOX_DIR]:
        try:
            for f in base_dir.iterdir():
                if f.is_file() and f.stat().st_mtime > since:
                    ext = f.suffix.lower()
                    if ext in _READABLE_EXTS and not f.name.startswith('.'):
                        files.append({"name": f.name, "size": f.stat().st_size})
        except Exception:
            pass
    return sorted(files, key=lambda x: x["name"])

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
    import time
    chat_start = time.time()

    # 打开一个连接，贯穿整个请求生命周期
    conn = _get_db()
    try:
        user_msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_msg_id, session_id, "user", req.message, now)
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        conn.commit()

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

        # 清理工具调用标签（XML + [调用 ...] 中文格式）
        if reply_text and ('调用' in reply_text or '<tool_calls' in reply_text):
            reply_text = re.sub(r'\[调用\s+\w+\][^{]*(?:\{[^}]*\})?', '', reply_text)
            reply_text = re.sub(r'<\s*\|?\s*tool_calls\b[^>]*>.*?</\s*\|?\s*tool_calls[^>]*>', '', reply_text, flags=re.DOTALL)
            reply_text = re.sub(r'<\s*\|?\s*(invoke|parameter|thinking)\b[^>]*>.*?</\s*\|?\s*(invoke|parameter|thinking)[^>]*>', '', reply_text, flags=re.DOTALL)

        # 保存史佚回复（使用同一个 conn，无 WAL 可见性问题）
        reply_msg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (reply_msg_id, session_id, "shiyi", reply_text, datetime.datetime.utcnow().isoformat())
        )
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (datetime.datetime.utcnow().isoformat(), session_id))
        conn.commit()
    finally:
        conn.close()

    # 处理 MEDIA: 标签 — 复制文件到 workspace
    reply_text, media_files = _process_media_in_reply(reply_text)

    # 扫描对话期间新写入的 workspace 文件
    new_files = _scan_new_workspace_files(chat_start)
    # 合并 MEDIA 文件（去重 — 同名取 workspace 扫描结果优先）
    seen_names = {f["name"] for f in new_files}
    for mf in media_files:
        if mf["name"] not in seen_names:
            new_files.append(mf)

    return ChatResponse(reply=reply_text, status=status, files=new_files)


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话接口 — SSE 逐 token 返回"""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    session_id = _resolve_session(req.session_id)
    now = datetime.datetime.utcnow().isoformat()
    import time
    chat_start = time.time()

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

    from fastapi.responses import StreamingResponse

    shiyi = get_shiyi()
    if not shiyi.llm_available:
        def _unavailable():
            yield f"data: {json.dumps({'type': 'error', 'content': 'LLM服务不可用，请检查API配置。'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_unavailable(), media_type="text/event-stream")

    platform_ctx = _build_platform_context(shiyi)

    def _stream_generator():
        full_reply = ""
        media_files = []
        try:
            for token in shiyi.chat_stream(req.message, platform_context=platform_ctx):
                full_reply += token
                yield f"data: {json.dumps({'type': 'token', 'content': token}, ensure_ascii=False)}\n\n"
        except LLMUnavailableError as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': f'错误: {e}'}, ensure_ascii=False)}\n\n"

        # 清理工具调用标签（XML + [调用 ...] 中文格式）
        if full_reply:
            import re as _re
            _stripped = _re.sub(r'\[调用\s+\w+\][^{]*(?:\{[^}]*\})?', '', full_reply)
            _stripped = _re.sub(r'<\s*\|?\s*tool_calls\b[^>]*>.*?</\s*\|?\s*tool_calls[^>]*>', '', _stripped, flags=_re.DOTALL)
            _stripped = _re.sub(r'<\s*\|?\s*(invoke|parameter|thinking)\b[^>]*>.*?</\s*\|?\s*(invoke|parameter|thinking)[^>]*>', '', _stripped, flags=_re.DOTALL)
            _stripped = _re.sub(r'<\s*\|?\s*(tool_calls|invoke|parameter|thinking)\b[^>]*/?>', '', _stripped)
            if _stripped != full_reply:
                full_reply = _stripped

        # 处理 MEDIA: 标签 — 复制文件到 workspace
        if full_reply and 'MEDIA:' in full_reply:
            cleaned_reply, media_files = _process_media_in_reply(full_reply)
            if cleaned_reply != full_reply:
                full_reply = cleaned_reply

        # 保存完整回复到 DB（已清除 MEDIA 标签）
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

        # 扫描对话期间新写入的 workspace 文件
        new_files = _scan_new_workspace_files(chat_start)
        # 合并 MEDIA 文件
        seen_names = {f["name"] for f in new_files}
        for mf in media_files:
            if mf["name"] not in seen_names:
                new_files.append(mf)

        yield f"data: {json.dumps({'type': 'done', 'content': full_reply, 'files': new_files}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_stream_generator(), media_type="text/event-stream")


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


@app.get("/api/export/conversations")
async def export_conversations(from_date: str = "", to_date: str = ""):
    """导出所有对话记录（含已关闭的对话），可按时间过滤

    返回格式：{ sessions: [{ id, name, messages: [{ role, content, created_at }] }] }
    """
    conn = _get_db()
    try:
        # 查询所有会话
        cur = conn.execute("SELECT id, name FROM sessions ORDER BY updated_at ASC")
        rows = cur.fetchall()
        sessions = []
        for row in rows:
            sid = row["id"]
            sname = row["name"]
            # 查询消息（带时间过滤）
            if from_date or to_date:
                sql = "SELECT role, content, created_at FROM messages WHERE session_id=?"
                params: list = [sid]
                if from_date:
                    sql += " AND created_at >= ?"
                    params.append(from_date)
                if to_date:
                    sql += " AND created_at <= ?"
                    params.append(to_date + "T23:59:59")
                sql += " ORDER BY created_at ASC"
                msg_cur = conn.execute(sql, params)
            else:
                msg_cur = conn.execute(
                    "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY created_at ASC",
                    (sid,),
                )
            msgs = [
                {"role": m["role"], "content": m["content"], "created_at": m["created_at"]}
                for m in msg_cur.fetchall()
            ]
            if msgs:
                sessions.append({"id": sid, "name": sname, "messages": msgs})
        return {"sessions": sessions, "total_sessions": len(sessions)}
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

@app.post("/api/chat/cancel")
async def chat_cancel():
    """取消正在进行的 chat() 工具循环"""
    shiyi = get_shiyi()
    shiyi.cancel_chat()
    return {"status": "cancelled"}

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
        with open(config_file, encoding="utf-8") as f:
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
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _write_user_config(clerk_id: str, config: dict):
    """写入吏员的用户配置"""
    path = _get_clerk_user_config_path(clerk_id)
    with open(path, "w", encoding="utf-8") as f:
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
    api_keys: list = []  # ["KEY_NAME", ...]
    model_name: str = ""  # LLM模型名称（空则自动使用主 LLM 配置）
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

    lines.append("\n## 工具使用规则（重要）")
    lines.append("你的回复中**不要模拟或假装执行操作**。对于所有实际操作（文件读写、命令执行、网络请求等），你**必须**调用对应的工具函数，而不是在文本中说\"我已经创建了文件\"。")
    lines.append("只有当工具返回结果后，你才能根据结果告诉用户发生了什么。")
    lines.append("对于文件操作：调用 file_write 工具，路径使用相对路径（如 workspace/xxx.txt），网关会自动发送。")
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
    """创建新吏员（工具由全局共享池提供，无需指定）"""
    from shiyi.core.clerk_creator import ClerkCreator
    creator = ClerkCreator()

    result = creator.create_non_interactive(
        name=req.name,
        desc=req.description,
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
            if hasattr(shiyi, 'clerk_registry'):
                worker = shiyi.clerk_registry.get_clerk(clerk_id)
                if worker:
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
    base_url: str = ""
    api_keys: list = []

@app.post("/api/clerks/{clerk_id}/configure")
async def configure_clerk(clerk_id: str, req: ClerkConfigureReq):
    """修改吏员配置（描述、模型、Base URL、API keys 等）"""
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
        if req.model_name:
            config["model_name"] = req.model_name
            # 同步更新 llm_config
            if "llm_config" not in config:
                config["llm_config"] = {}
            config["llm_config"]["model"] = req.model_name
        if req.base_url:
            if "llm_config" not in config:
                config["llm_config"] = {}
            config["llm_config"]["base_url"] = req.base_url
        if req.api_keys:
            # Support new format: [{name, env, value}, ...]
            processed_keys = []
            for k in req.api_keys:
                if isinstance(k, dict) and k.get("value"):
                    # Save value to env var or clerk.json
                    processed_keys.append({"name": k.get("name", ""), "env": k.get("env", k.get("name", ""))})
                    # Write actual key value to environment-like storage
                    import os
                    env_name = k.get("env", k.get("name", ""))
                    if env_name:
                        os.environ[env_name] = k["value"]
                elif isinstance(k, dict):
                    processed_keys.append(k)
                else:
                    processed_keys.append(k)
            config["api_keys"] = processed_keys

        with open(cj_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        return {"status": "ok", "message": f"吏员 {clerk_id} 配置已更新"}
    except Exception as e:
        raise HTTPException(500, f"配置修改失败: {str(e)}")

class ClerkSkillAssignReq(BaseModel):
    skills: list  # ["skill_id1", "skill_id2", ...]

@app.post("/api/clerks/{clerk_id}/skills")
async def assign_clerk_skills(clerk_id: str, req: ClerkSkillAssignReq):
    """分配技能到吏员（写入 clerk.json → 热刷新 registry）"""
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
        
        # 热刷新：让内存中的 SkillRegistry 感知变更
        try:
            shiyi = get_shiyi()
            if hasattr(shiyi, '_skill_registry') and shiyi._skill_registry is not None:
                shiyi._skill_registry.refresh()
        except Exception:
            pass  # 容错：刷新失败不影响落盘
        
        return {"status": "ok", "message": "技能已更新（registry 已刷新）", "skills": req.skills}
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
                desc = sinfo.description if hasattr(sinfo, 'description') else ""
                desc_zh = sinfo.description_zh if hasattr(sinfo, 'description_zh') else ""
                cat = sinfo.category if hasattr(sinfo, 'category') else ""
                # 如果 SKILL.md 没有 description_zh，使用快速翻译兜底
                if not desc_zh and desc:
                    desc_zh = _quick_translate_description(desc, cat)
                skills.append({
                    "id": skill_id,
                    "name": sinfo.name if hasattr(sinfo, 'name') else skill_id,
                    "description": desc,
                    "description_zh": desc_zh,
                    "category": cat,
                    "path": sinfo.path if hasattr(sinfo, 'path') else "",
                    "triggers": sinfo.triggers if hasattr(sinfo, 'triggers') else [],
                    "keywords": sinfo.keywords if hasattr(sinfo, 'keywords') else [],
                })
    except Exception as e:
        return {"skills": [], "error": str(e)}
    return {"skills": skills}


@app.get("/api/skills/available")
async def search_available_skills(query: str = ""):
    """搜索可用技能（SkillHub 多源搜索 + 区分已安装/未安装）"""
    try:
        shiyi = get_shiyi()
        hub = shiyi._skill_hub

        # 已安装的 skill ID 集合
        installed = set()
        if hasattr(shiyi, '_skill_loader') and shiyi._skill_loader:
            installed = set(shiyi._skill_loader._skills.keys())

        # 中文查询：直接传原始中文，让 SkillHub._match() 内置的 cn_to_en 映射处理
        # 不再在 webui 层做预扩展，避免双重扩展导致 OR 逻辑匹配过多
        search_query = query

        # 通过 SkillHub 搜索（在 executor 中执行避免阻塞事件循环）
        available = []
        if search_query.strip():
            loop = asyncio.get_event_loop()
            search_result = await loop.run_in_executor(None, hub.search, search_query, 20)
            for entry in search_result.entries:
                entry_dict = {
                    "skill_id": entry.skill_id,
                    "name": entry.name,
                    "description": entry.description,
                    "description_zh": _quick_translate_description(entry.description, entry.category),
                    "category": entry.category,
                    "source": entry.source,
                    "install_url": entry.install_url,
                    "homepage": entry.homepage,
                }
                available.append(entry_dict)

        return {
            "installed": sorted(installed),
            "available": available,
            "search_query": query,
            "mapped_query": _chinese_to_english_keywords(query) if _contains_chinese(query) else "",
            "total_available": len(available),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/skills/install")
async def install_skill(req: dict):
    """安装技能：从 SkillHub 下载 SKILL.md → 翻译 → 刷新 registry"""
    try:
        skill_id = req.get("skill_id", "")
        source = req.get("source", "webui")
        if not skill_id:
            raise HTTPException(status_code=400, detail="skill_id 不能为空")

        shiyi = get_shiyi()
        hub = shiyi._skill_hub

        # Run install in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        ok, msg = await loop.run_in_executor(None, hub.install, skill_id, source)
        if not ok:
            raise HTTPException(status_code=500, detail=f"安装失败: {msg}")

        # 立即写入快速翻译的 description_zh 到 SKILL.md（同步，不阻塞）
        _write_quick_description_zh(skill_id)

        # 后台线程做 LLM 翻译 description_zh 和 body_zh（更高质量但慢）
        _translate_skill_after_install(shiyi, skill_id)

        # 刷新 SkillLoader + SkillRegistry
        if hasattr(shiyi, '_skill_loader') and shiyi._skill_loader:
            shiyi._skill_loader.scan()
        if hasattr(shiyi, '_skill_registry') and shiyi._skill_registry:
            shiyi._skill_registry.refresh()

        return {"status": "ok", "skill_id": skill_id, "message": f"安装成功: {msg}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
                "description_zh": sinfo.description_zh if hasattr(sinfo, 'description_zh') else "",
                "body_zh": sinfo.body_zh if hasattr(sinfo, 'body_zh') else "",
                "category": sinfo.category if hasattr(sinfo, 'category') else "",
                "path": sinfo.path if hasattr(sinfo, 'path') else "",
                "triggers": sinfo.triggers if hasattr(sinfo, 'triggers') else [],
                "keywords": sinfo.keywords if hasattr(sinfo, 'keywords') else [],
                "raw_body": sinfo.raw_body if hasattr(sinfo, 'raw_body') else "",
            }
    except Exception as e:
        raise HTTPException(404, f"Skill {skill_id} 未找到: {str(e)}")
    raise HTTPException(404, f"Skill {skill_id} 未找到")


@app.delete("/api/skills/{skill_id:path}")
async def uninstall_skill(skill_id: str):
    """卸载技能：删除 SKILL.md → 刷新 registry"""
    try:
        shiyi = get_shiyi()
        hub = shiyi._skill_hub
        ok, msg = hub.uninstall(skill_id)
        if not ok:
            raise HTTPException(status_code=404, detail=msg)

        # 刷新 SkillLoader + SkillRegistry
        if hasattr(shiyi, '_skill_loader') and shiyi._skill_loader:
            shiyi._skill_loader.scan()
        if hasattr(shiyi, '_skill_registry') and shiyi._skill_registry:
            shiyi._skill_registry.refresh()

        return {"status": "ok", "skill_id": skill_id, "message": msg}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════
# 全局工具 API（中文描述）
# ═══════════════════════════════════════════

# 工具中文描述映射（内置）
_TOOL_DESCRIPTIONS_ZH = {
    "bash": "执行 Shell 命令，运行终端指令",
    "file_read": "读取文件内容",
    "file_write": "写入文件到工作区",
    "file_list": "列出目录下的文件",
    "web_search": "搜索互联网信息",
    "web_fetch": "获取网页内容",
    "memory_search": "搜索对话记忆",
    "memory_save": "保存记忆条目",
    "create_clerk": "创建新的吏员",
    "recall_memory": "回忆历史对话内容",
    "search_conversations": "搜索对话历史记录",
}

@app.get("/api/tools")
async def list_tools():
    """列出所有全局工具（含中文描述）"""
    shiyi = get_shiyi()
    tools = []
    seen = set()
    schemas = shiyi.clerk_registry.get_schemas()
    for s in schemas:
        func = s.get("function", {})
        name = func.get("name", "")
        if name in seen:
            continue
        seen.add(name)
        desc = func.get("description", "")
        desc_zh = _TOOL_DESCRIPTIONS_ZH.get(name, "")
        # 如果没有内置映射，尝试从描述推断
        if not desc_zh and desc and shiyi._llm and shiyi._llm.is_available():
            pass  # 不实时翻译，太慢
        params = func.get("parameters", {})
        # 为参数也添加中文描述
        params_zh = {}
        if isinstance(params, dict):
            props = params.get("properties", {})
            for pname, pval in props.items():
                p_desc = pval.get("description", "")
                if p_desc:
                    params_zh[pname] = p_desc
        tools.append({
            "name": name,
            "description": desc,
            "description_zh": desc_zh,
            "parameters": params,
            "parameters_zh": params_zh,
        })
    return {"tools": tools, "total": len(tools)}


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

# ═══════════════════════════════════════════
# 配置 API
# ═══════════════════════════════════════════

@app.get("/api/main-llm-config")
async def get_main_llm_config():
    """返回主 LLM 配置信息（供吏员创建时自动填充，不返回 API Key）"""
    return {
        "provider": os.environ.get("SHIYI_MAIN_LLM_PROVIDER", DEFAULT_LLM_PROVIDER),
        "model": os.environ.get("SHIYI_MAIN_LLM_MODEL", DEFAULT_MAIN_LLM_MODEL),
        "base_url": os.environ.get("SHIYI_MAIN_API_BASE", DEFAULT_LLM_BASE_URL),
        "key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
    }

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
        "name": "微信（个人号）",
        "icon": "💬",
        "fields": [
            {"key": "account_id", "label": "Account ID (iLink Bot ID)", "secret": False, "placeholder": "扫码登录后自动获取"},
            {"key": "ilink_token", "label": "iLink Token", "secret": True, "placeholder": "扫码登录后自动获取"},
            {"key": "base_url", "label": "iLink API 地址", "secret": False, "placeholder": "https://ilinkai.weixin.qq.com"},
        ],
        "scopes": [],
        "event": "iLink Bot 长轮询",
        "guide_link": "https://github.com/anty0418/shiyi-tongxun",
        "guide_steps": [
            {"step": 1, "title": "安装依赖", "desc": "1) 运行 pip install cryptography certifi qrcode\n2) cryptography 用于微信CDN加解密\n3) qrcode 用于终端显示二维码（可选）"},
            {"step": 2, "title": "扫码登录", "desc": "1) 点击下方「扫码登录」按钮\n2) 在弹出窗口中，使用微信扫描显示的二维码\n3) 在微信中确认授权\n4) 登录成功后 Account ID 和 Token 会自动保存"},
            {"step": 3, "title": "启动网关", "desc": "1) 在服务器上运行：python -m shiyi.shell.gateway.run wechat\n2) 确认日志显示「WeChat adapter started」\n3) 用另一个微信号给登录的微信号发消息测试连通性"},
            {"step": 4, "title": "注意事项", "desc": "1) 微信个人号使用 iLink Bot API，无需公网域名\n2) Token 保存在 ~/.shiyi/weixin/ 目录下\n3) 如 Token 过期，需重新扫码登录\n4) 默认仅接收私聊消息，群聊需额外配置 group_policy"},
        ],
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

    elif channel_id == "wechat":
        account_id = ch_cfg.get("account_id", "")
        ilink_token = ch_cfg.get("ilink_token", "")
        # Try to load saved credentials
        if not ilink_token and account_id:
            try:
                from shiyi.shell.gateway.adapters.wechat import load_weixin_account
                persisted = load_weixin_account(account_id)
                if persisted:
                    ilink_token = str(persisted.get("token") or "")
            except Exception:
                pass
        if not account_id or not ilink_token:
            return {"status": "error", "message": "请先扫码登录获取 Account ID 和 Token"}
        try:
            import urllib.request
            base_url = ch_cfg.get("base_url", "https://ilinkai.weixin.qq.com")
            from shiyi.shell.gateway.adapters.wechat import _get_config_api
            resp = _get_config_api(base_url, ilink_token, account_id)
            if resp.get("ret") in {0, None}:
                return {"status": "ok", "message": "微信连接成功 ✅"}
            else:
                return {"status": "error", "message": f"认证失败: ret={resp.get('ret')} errmsg={resp.get('errmsg', '未知')}"}
        except Exception as e:
            return {"status": "error", "message": f"连接失败: {str(e)}"}

    return {"status": "ok", "message": "测试功能暂未实现"}


# ── 微信扫码登录两步流程 ──
_wechat_qr_session: dict = {}  # 存储当前 QR 会话: {qrcode_value, qrcode_img_url, base_url}


@app.post("/api/channels/wechat/qr-init")
async def wechat_qr_init():
    """Fetch QR code from iLink and return image URL for browser display."""
    global _wechat_qr_session
    try:
        from shiyi.shell.gateway.adapters.wechat import (
            ILINK_BASE_URL, EP_GET_BOT_QR, QR_TIMEOUT_MS,
            _http_get as _ilink_get,
        )
        loop = asyncio.get_event_loop()
        qr_resp = await loop.run_in_executor(
            None,
            lambda: _ilink_get(
                f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}?bot_type=3",
                timeout=QR_TIMEOUT_MS / 1000,
            ),
        )
        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_img_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            return {"status": "error", "message": "iLink 未返回二维码"}

        _wechat_qr_session = {
            "qrcode_value": qrcode_value,
            "qrcode_img_url": qrcode_img_url,
            "base_url": ILINK_BASE_URL,
        }
        return {
            "status": "ok",
            "qrcode_value": qrcode_value,
            "qrcode_img_url": qrcode_img_url,
        }
    except ImportError as e:
        return {"status": "error", "message": f"微信模块未安装: {e}"}
    except ConnectionError as e:
        return {"status": "error", "message": f"无法连接 iLink 服务器: {e}"}
    except TimeoutError:
        return {"status": "error", "message": "连接 iLink 超时，请检查网络"}
    except RuntimeError as e:
        return {"status": "error", "message": f"iLink API 错误: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"获取二维码失败: {type(e).__name__}: {e}"}


@app.get("/api/channels/wechat/qr-image")
async def wechat_qr_image(value: str = ""):
    """Generate a real QR code PNG image from a qrcode_value (liteapp URL)."""
    if not value:
        from fastapi.responses import Response
        return Response(status_code=400)
    try:
        import qrcode as qrcode_lib
        import io
        url = f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode={value}&bot_type=3"
        img = qrcode_lib.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        from fastapi.responses import Response
        return Response(content=buf.getvalue(), media_type="image/png")
    except Exception:
        from fastapi.responses import Response
        return Response(status_code=500)


@app.get("/api/channels/wechat/qr-status")
async def wechat_qr_status():
    """Poll QR scan status. On confirmation, save credentials automatically."""
    global _wechat_qr_session
    if not _wechat_qr_session.get("qrcode_value"):
        return {"status": "error", "message": "请先获取二维码"}

    try:
        from shiyi.shell.gateway.adapters.wechat import (
            EP_GET_QR_STATUS, QR_TIMEOUT_MS,
            _http_get as _ilink_get, save_weixin_account,
        )
        qrcode_value = _wechat_qr_session["qrcode_value"]
        base_url = _wechat_qr_session.get("base_url", "https://ilinkai.weixin.qq.com")

        loop = asyncio.get_event_loop()
        status_resp = await loop.run_in_executor(
            None,
            lambda: _ilink_get(
                f"{base_url}/{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                timeout=QR_TIMEOUT_MS / 1000,
            ),
        )

        scan_status = str(status_resp.get("status") or "wait")

        # Handle redirect
        if scan_status == "scaned_but_redirect":
            redirect_host = str(status_resp.get("redirect_host") or "")
            if redirect_host:
                _wechat_qr_session["base_url"] = f"https://{redirect_host}"

        # Handle expired — try to refresh QR
        if scan_status == "expired":
            try:
                from shiyi.shell.gateway.adapters.wechat import (
                    ILINK_BASE_URL, EP_GET_BOT_QR,
                )
                new_resp = await loop.run_in_executor(
                    None,
                    lambda: _ilink_get(
                        f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}?bot_type=3",
                        timeout=QR_TIMEOUT_MS / 1000,
                    ),
                )
                new_value = str(new_resp.get("qrcode") or "")
                new_img = str(new_resp.get("qrcode_img_content") or "")
                if new_value:
                    _wechat_qr_session["qrcode_value"] = new_value
                    _wechat_qr_session["qrcode_img_url"] = new_img
                    return {
                        "status": "expired",
                        "qrcode_value": new_value,
                        "qrcode_img_url": new_img,
                        "message": "二维码已刷新，请重新扫描",
                    }
            except Exception:
                pass
            return {"status": "expired", "message": "二维码已过期，请重新获取"}

        # Handle confirmed — save credentials
        if scan_status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            token = str(status_resp.get("bot_token") or "")
            result_base_url = str(status_resp.get("baseurl") or base_url)
            user_id = str(status_resp.get("ilink_user_id") or "")
            if not account_id or not token:
                return {"status": "error", "message": "登录凭据不完整"}

            save_weixin_account(
                account_id=account_id,
                token=token,
                base_url=result_base_url,
                user_id=user_id,
            )
            gw = _load_gateway_yaml()
            gw["wechat"] = gw.get("wechat", {})
            gw["wechat"]["account_id"] = account_id
            gw["wechat"]["ilink_token"] = token
            if result_base_url:
                gw["wechat"]["base_url"] = result_base_url
            _save_gateway_yaml(gw)

            _wechat_qr_session = {}  # Clear session
            return {
                "status": "confirmed",
                "message": f"微信登录成功！Account: {account_id[:8]}...",
            }

        # Other statuses: wait, scaned
        return {"status": scan_status}

    except Exception as e:
        return {"status": "error", "message": f"状态查询失败: {str(e)}"}


@app.post("/api/gateway/wechat/start")
async def start_wechat_gateway():
    """Start the WeChat gateway in a background thread."""
    import threading
    try:
        from shiyi.shell.gateway.run import run as gateway_run
        shiyi = get_shiyi()
        t = threading.Thread(target=gateway_run, args=("wechat", shiyi),
                             daemon=True, name="gateway-wechat")
        t.start()
        return {"status": "ok", "message": "微信网关已启动"}
    except Exception as e:
        return {"status": "error", "message": f"网关启动失败: {str(e)}"}


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
async def get_memories(query: str = "", domain: str = "", layer: str = "", limit: int = 20, offset: int = 0):
    """记忆晶体概览 + 最近记录 / 搜索

    Args:
        query: 搜索关键词
        domain: 按领域过滤（如 "technology", "personal"）
        layer: 按层级过滤（"hot" 或 "cold"）
        limit: 返回条数
        offset: 偏移量（分页）
    """
    s = get_shiyi()
    stats = {"total_fragments": 0, "hot_count": 0, "cold_count": 0, "domains": []}
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
                # Collect domain list
                domain_stats = frag_stats.get("by_domain", {})
                stats["domains"] = sorted(domain_stats.keys()) if domain_stats else []

            if hasattr(mem, 'recall'):
                raw = mem.recall(query or "", top_k=limit + offset)
                if isinstance(raw, list):
                    for item in raw:
                        frag = item.get("fragment", {}) if isinstance(item, dict) else None
                        entry = _extract_fragment_info(frag, item) if frag else _extract_fragment_info_dict(item)
                        # Apply domain/layer filter
                        if domain and entry.get("domain", "").lower() != domain.lower():
                            continue
                        if layer and entry.get("layer", "").lower() != layer.lower():
                            continue
                        recent.append(entry)
                else:
                    recalled = raw.get("recalled", [])
                    for item in recalled:
                        entry = {
                            "content": item.get("content", item.get("fact", "")),
                            "domain": item.get("domain", ""),
                            "created_at": item.get("created_at", item.get("timestamp", "")),
                            "emotion": item.get("emotion", ""),
                            "layer": item.get("layer", ""),
                            "fragment_id": item.get("fragment_id", ""),
                        }
                        if domain and entry["domain"].lower() != domain.lower():
                            continue
                        if layer and entry["layer"].lower() != layer.lower():
                            continue
                        recent.append(entry)
    except Exception as e:
        return {"stats": stats, "recent": [], "results": [], "error": str(e)}

    # Paginate
    total_count = len(recent)
    paginated = recent[offset:offset + limit]

    # If query provided, return as results with scores
    if query:
        return {"stats": stats, "results": paginated, "total": total_count, "limit": limit, "offset": offset}

    return {"stats": stats, "recent": paginated, "total": total_count, "limit": limit, "offset": offset}


def _extract_fragment_info(frag, item: dict) -> dict:
    """Extract fragment info from a Fragment object + recall item dict."""
    scene = getattr(frag, "scene_shell", None)
    emotion = getattr(frag, "emotion_shell", None)
    return {
        "content": getattr(frag, "fact_kernel", "") or "",
        "domain": getattr(scene, "domain", "") if scene else "",
        "created_at": str(getattr(frag, "created_at", "") or ""),
        "emotion": getattr(emotion, "primary", "") if emotion else "",
        "layer": getattr(frag, "layer", "hot"),
        "fragment_id": getattr(frag, "fragment_id", ""),
        "score": item.get("score", 0) if isinstance(item, dict) else 0,
    }


def _extract_fragment_info_dict(item: dict) -> dict:
    """Extract fragment info from a plain dict (fallback)."""
    return {
        "content": str(item.get("content", item.get("fact", ""))),
        "domain": str(item.get("domain", "")),
        "created_at": str(item.get("created_at", item.get("timestamp", ""))),
        "emotion": str(item.get("emotion", item.get("emotion_primary", ""))),
        "layer": str(item.get("layer", "hot")),
        "fragment_id": str(item.get("fragment_id", "")),
        "score": item.get("score", 0),
    }


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
PORT = 8530

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

    # Check wechat
    wechat_cfg = config.get("wechat", {})
    if wechat_cfg.get("ilink_token") or wechat_cfg.get("account_id"):
        print("检测到微信配置，自动启动微信网关...")
        try:
            from shiyi.shell.gateway.run import run as gateway_run
            t = threading.Thread(target=gateway_run, args=("wechat", shiyi_instance),
                                 daemon=True, name="gateway-wechat")
            t.start()
            print("微信网关已启动（后台线程）")
        except Exception as e:
            print(f"微信网关启动失败: {e}")


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
        if not shiyi.llm_available:
            print()
            print("⚠️  API Key 未配置，当前为 Mock 模式。")
            print("   请在浏览器中打开设置页面，配置以下任一 API Key：")
            print("   - DEEPSEEK_API_KEY (DeepSeek)")
            print("   - SILICONFLOW_API_KEY (硅基流动)")
            print("   - OPENAI_API_KEY (OpenAI 兼容)")
            print()
        
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
