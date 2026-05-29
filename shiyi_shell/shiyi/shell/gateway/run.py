"""Gateway runner — wires platform adapter → Shiyi engine.

Usage: python -m shiyi.shell.gateway.run feishu
"""

import atexit
import hashlib
import logging
import os
import sys
import time

from .base import MessageEvent
from .config import load_feishu_config
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
            os.kill(old_pid, 0)  # signal 0 = probe
            logger.warning(
                "Found existing gateway (PID %d), terminating...", old_pid
            )
            try:
                os.kill(old_pid, 15)  # SIGTERM
                import time as _time
                for _ in range(30):  # wait up to 3s
                    _time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(old_pid, 9)  # SIGKILL
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


_WORKSPACE_DIR = os.path.expanduser("~/.shiyi/workspace")


def _try_send_workspace_file(adapter, conversation_id: str) -> None:
    """Scan workspace for the most recently written file and send it via Feishu.

    Only sends .txt/.json/.csv/.md/.log files written within the last 60 seconds.
    Marker is only touched AFTER a successful upload, so failed sends are retried.
    """
    if not hasattr(adapter, 'upload_file'):
        logger.debug("_try_send_workspace_file: adapter lacks upload_file method")
        return

    sent_marker = os.path.join(_WORKSPACE_DIR, ".gateway_last_sent")
    last_sent_mtime = 0.0
    try:
        last_sent_mtime = os.path.getmtime(sent_marker)
    except OSError:
        pass

    try:
        files = []
        for fn in os.listdir(_WORKSPACE_DIR):
            if fn.startswith('.'):
                continue
            fpath = os.path.join(_WORKSPACE_DIR, fn)
            if not os.path.isfile(fpath):
                continue
            ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
            if ext not in ('txt', 'json', 'csv', 'log', 'md'):
                continue
            mtime = os.path.getmtime(fpath)
            if mtime > last_sent_mtime:
                files.append((mtime, fpath))
    except FileNotFoundError:
        return

    if not files:
        logger.debug("_try_send_workspace_file: no files newer than marker (last_sent=%.0fs ago)",
                     time.time() - last_sent_mtime)
        return

    # Pick the most recently modified file
    files.sort(reverse=True)
    mtime, fpath = files[0]
    age = time.time() - mtime

    if age > 60:  # ignore stale files
        logger.debug("_try_send_workspace_file: newest file %s is %.0fs old, skipping",
                     os.path.basename(fpath), age)
        return

    try:
        file_key = adapter.upload_file(fpath)
        if file_key:
            adapter.send_file(conversation_id, file_key, os.path.basename(fpath))
            # Only update marker AFTER successful upload
            with open(sent_marker, 'w') as f:
                f.write(str(int(time.time())))
            logger.info("Auto-sent workspace file: %s", fpath)
        else:
            logger.warning("upload_file returned None for %s", os.path.basename(fpath))
    except Exception:
        logger.exception("Failed to auto-send workspace file: %s", fpath)


def run(platform: str, shiyi) -> None:
    """Main event loop: adapter messages → Shiyi.chat() → reply.

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
            # 构建平台上下文
            tools = shiyi._clerk_registry.get_schemas()
            tool_lines = []
            for t in tools:
                fn = t.get("function", {})
                name = fn.get("name", "")
                desc = fn.get("description", "")
                tool_lines.append(f"  - {name}: {desc}")
            tool_list = "\n".join(tool_lines) if tool_lines else "  （未见注册工具）"
            # 列出已注册的吏员
            clerk_lines = []
            try:
                for cid, cdata in shiyi._clerk_registry._clerks.items():
                    worker = cdata.get("worker")
                    name = getattr(getattr(worker, 'config', None), 'name', cid) if worker else cid
                    clerk_lines.append(f"  - {name}")
            except Exception:
                clerk_lines.append("  （信息获取失败）")
            clerk_list = "\n".join(clerk_lines) if clerk_lines else "  （无）"

            platform_ctx = f"""当前渠道: 飞书（用户通过飞书App访问）。文件写入后网关会自动发送给用户。

你拥有吏员系统（Clerks），吏员是执行具体任务的独立模块。你不是吏员，你是史佚本身。吏员提供工具给你使用，但工具不等同于吏员。

当前已注册吏员:
{clerk_list}

可用工具（由吏员提供）:
{tool_list}

关于吏员：
- 吏员由用户在WebUI的"吏员"面板中创建和管理，你本人无法直接创建吏员
- 如果用户想创建吏员，请引导他们前往WebUI的"吏员"面板操作"""

            # Route through Shiyi engine — full memory + conversation chain
            reply = shiyi.chat(event.content, conversation_id=event.conversation_id,
                               platform_context=platform_ctx)
        except Exception:
            logger.exception("Shiyi.chat() failed")
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
