"""Gateway runner — wires platform adapter → Shiyi engine.

Usage: python -m shiyi.shell.gateway.run feishu
"""

import atexit
import hashlib
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

from .base import MessageEvent
from .config import load_feishu_config, load_wechat_config
from .adapters import ADAPTERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("shiyi.gateway")

# ── PID lock: prevent multiple gateway instances ──
_PID_FILE = os.path.expanduser("~/.shiyi/gateway.pid")


def _acquire_pid_lock() -> bool:
    """Write PID file. Kills zombie gateways from previous installs automatically."""
    _DEDUP_FILE = os.path.expanduser("~/.shiyi/gateway_dedup.json")
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            if old_pid == os.getpid():
                return True  # already own the lock
            os.kill(old_pid, 0)  # signal 0 = probe
            logger.warning(
                "Found existing gateway (PID %d), terminating...", old_pid
            )
            try:
                os.kill(old_pid, signal.SIGTERM)
                import time as _time
                for _ in range(30):  # wait up to 3s
                    _time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    # Force kill: on Windows, SIGTERM is the strongest signal
                    if os.name == "nt":
                        os.kill(old_pid, signal.SIGTERM)
                    else:
                        os.kill(old_pid, signal.SIGKILL)
                    _time.sleep(0.3)
            except ProcessLookupError:
                pass  # already dead
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock — old process is dead
        # Clean up PID file only — dedup state must survive across restarts
        # otherwise Feishu WS reconnection replays all messages and the gateway
        # spams the user with duplicate replies
        try:
            os.remove(_PID_FILE)
        except FileNotFoundError:
            pass
    with open(_PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    atexit.register(_release_pid_lock)
    return True


def _release_pid_lock() -> None:
    """Release PID lock on exit. Dedup file is preserved across restarts."""
    try:
        if os.path.exists(_PID_FILE):
            os.remove(_PID_FILE)
    except Exception:
        pass


_LAST_WRITTEN_TRACKER = os.path.expanduser("~/.shiyi/.last_written_file")

# File extensions eligible for MEDIA: delivery
_MEDIA_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.mov', '.avi',
               '.mkv', '.webm', '.3gp', '.mp3', '.ogg', '.wav', '.silk', '.amr',
               '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
               '.txt', '.md', '.json', '.yaml', '.yml', '.toml', '.csv', '.xml',
               '.html', '.htm', '.py', '.js', '.ts', '.zip', '.tar', '.gz',
               '.tar.gz', '.bz2', '.7z', '.log'}

_MEDIA_RE = re.compile(
    r'[`"\']?MEDIA:\s*((?:[A-Za-z]:[/\\]|/|~/)\S+\.(?:' +
    '|'.join(ext.lstrip('.') for ext in _MEDIA_EXTS) +
    r'))\b',
    re.IGNORECASE
)


def _deliver_media_files(response: str, adapter, conversation_id: str) -> None:
    """Parse MEDIA: tags from LLM response and deliver as native files.

    Scans the LLM response
    text for MEDIA: tags, extracts file paths, then uploads and sends each one
    via the platform adapter.
    """
    if not hasattr(adapter, 'upload_file') or 'MEDIA:' not in response:
        return

    paths_seen = set()
    for match in _MEDIA_RE.finditer(response):
        path = match.group(1).strip()
        # Strip surrounding quotes/backticks
        if len(path) >= 2 and path[0] == path[-1] and path[0] in '`"\'`':
            path = path[1:-1]
        path = path.lstrip('`"\'').rstrip('`"\',.;:)}\\')
        path = os.path.expanduser(path)

        if not path or path in paths_seen:
            continue
        if not os.path.isfile(path):
            logger.warning("MEDIA: file not found: %s", path)
            continue

        paths_seen.add(path)
        try:
            file_key = adapter.upload_file(path, to_user_id=conversation_id)
            if file_key:
                adapter.send_file(conversation_id, file_key, os.path.basename(path))
                logger.info("MEDIA delivered: %s", path)
            else:
                logger.warning("MEDIA upload failed: %s", os.path.basename(path))
        except Exception:
            logger.exception("MEDIA delivery failed: %s", path)


def _try_send_workspace_file(adapter, conversation_id: str) -> None:
    """Send the file most recently written by a clerk tool.

    Reads ~/.shiyi/.last_written_file (set by file_write / enhanced_file_write).
    Sends the file via adapter, then clears the tracker so it isn't re-sent.
    """
    if not hasattr(adapter, 'upload_file'):
        return

    try:
        filepath = Path(_LAST_WRITTEN_TRACKER).read_text().strip()
    except Exception:
        return
    if not filepath or not os.path.isfile(filepath):
        return

    # Atomically clear tracker so we never double-send
    Path(_LAST_WRITTEN_TRACKER).write_text("")

    try:
        file_key = adapter.upload_file(filepath, to_user_id=conversation_id)
        if file_key:
            adapter.send_file(conversation_id, file_key, os.path.basename(filepath))
            logger.info("Auto-sent file: %s", filepath)
        else:
            logger.warning("upload_file returned None for %s", os.path.basename(filepath))
    except Exception:
        logger.exception("Failed to auto-send file: %s", filepath)


def run(platform: str, shiyi) -> None:
    """Main event loop: adapter messages → Shiyi.talk() → reply.

    shiyi must be a fully initialized Shiyi instance.
    """
    # PID lock — prevent multiple gateway instances
    if not _acquire_pid_lock():
        sys.exit(1)

    adapter_cls = ADAPTERS.get(platform)
    if not adapter_cls:
        print(f"Unknown platform: {platform}. Available: {', '.join(ADAPTERS)}")
        sys.exit(1)

    # Load platform config
    if platform == "feishu":
        config = load_feishu_config()
        if not config.app_id or not config.app_secret:
            print("Feishu config missing. Set FEISHU_APP_ID / FEISHU_APP_SECRET env vars")
            print("or create ~/.shiyi/gateway.yaml with feishu.app_id and feishu.app_secret.")
            sys.exit(1)
    elif platform == "wechat":
        config = load_wechat_config()
        token = config.extra.get("token", "")
        account_id = config.extra.get("account_id", "")
        if not token or not account_id:
            print("WeChat iLink config missing. Run 'shiyi wechat-login' to set up,")
            print("or set WEIXIN_TOKEN / WEIXIN_ACCOUNT_ID env vars,")
            print("or create ~/.shiyi/gateway.yaml with wechat.account_id and wechat.ilink_token.")
            sys.exit(1)
    else:
        print(f"No config loader for {platform}")
        sys.exit(1)

    # Build adapter
    adapter = adapter_cls(config)

    # Content-based dedup: catch duplicates even if message_id is missing
    _last_seen = {}
    _DEDUP_WINDOW = 5

    def handle_message(event: MessageEvent) -> None:
        """Called by adapter when a user message arrives."""
        nonlocal _last_seen
        logger.info("Received: platform=%s user=%s text=%.100s", event.platform, event.user_id, event.content)

        content_hash = hashlib.sha256(event.content.encode()).hexdigest()
        dedup_key = (event.user_id, content_hash)
        now = time.time()
        last_ts = _last_seen.get(dedup_key, 0)
        if now - last_ts < _DEDUP_WINDOW:
            logger.warning("Dedup: skipping duplicate from %s within %ds", event.user_id, _DEDUP_WINDOW)
            return
        _last_seen[dedup_key] = now
        if len(_last_seen) > 200:
            _last_seen = {k: v for k, v in _last_seen.items() if v > now - _DEDUP_WINDOW * 2}

        try:
            # 构建平台上下文（使用公共 API）
            tools = shiyi.clerk_registry.get_schemas()
            tool_lines = []
            for t in tools:
                fn = t.get("function", {})
                name = fn.get("name", "")
                desc = fn.get("description", "")
                tool_lines.append(f"  - {name}: {desc}")
            tool_list = "\n".join(tool_lines) if tool_lines else "  （未见注册工具）"

            # 列出已注册的吏员
            clerklines = []
            try:
                for clerk in shiyi.clerk_registry.list_clerks():
                    clerklines.append(f"  - {clerk.get('name', clerk.get('id', '?'))}")
            except Exception:
                clerklines.append("  （信息获取失败）")
            clerk_list = "\n".join(clerklines) if clerklines else "  （无）"

            # 跨平台 MEDIA 路径示例
            if os.name == "nt":
                media_path_example = "%USERPROFILE%\\.shiyi\\workspace\\report.txt"
            else:
                media_path_example = "~/.shiyi/workspace/report.txt"

            platform_ctx = f"""当前渠道: {platform == 'feishu' and '飞书（用户通过飞书App访问）' or '微信（用户通过微信个人号私聊）'}。文件写入后网关会自动发送给用户。

你拥有吏员系统（Clerks），吏员是执行具体任务的独立模块。你不是吏员，你是史佚本身。吏员提供工具给你使用，但工具不等同于吏员。

当前已注册吏员:
{clerk_list}

可用工具（由吏员提供）:
{tool_list}

## 工具使用规则（重要）
你的回复中**不要模拟或假装执行操作**。对于所有实际操作（文件读写、命令执行、网络请求等），你**必须**调用对应的工具函数，而不是在文本中说"我已经创建了文件"。
只有当工具返回结果后，你才能根据结果告诉用户发生了什么。
对于文件操作：调用 file_write 工具，写入完成后在回复中添加 `MEDIA:文件绝对路径`（如 `MEDIA:{media_path_example}`），网关会自动识别并发送文件给用户。"""

            # Route through Shiyi engine — full memory + conversation chain
            # Use chat() (not talk()) so clerks get tools and can write files
            reply = shiyi.chat(event.content, conversation_id=event.conversation_id,
                               platform_context=platform_ctx)
        except Exception:
            logger.exception("Shiyi.talk() failed")
            reply = "抱歉，我暂时无法回复，请稍后再试。"

        if reply:
            try:
                # Use send_long for auto-chunking of long replies
                if hasattr(adapter, 'send_long'):
                    adapter.send_long(event.conversation_id, reply)
                else:
                    adapter.send(event.conversation_id, reply)
                logger.info("Replied to %s: %.80s", event.user_id, reply)
            except Exception:
                logger.exception("adapter.send() failed")

            # Auto-upload any file the LLM just wrote to workspace
            _try_send_workspace_file(adapter, event.conversation_id)
            # Also deliver files via MEDIA: tags
            _deliver_media_files(reply, adapter, event.conversation_id)

    print(f"Starting {platform} gateway...")
    adapter.start(on_message=handle_message)

    try:
        # Keep main thread alive; adapter runs in background thread
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down gateway...")
        adapter.stop()
        print("Gateway stopped.")
