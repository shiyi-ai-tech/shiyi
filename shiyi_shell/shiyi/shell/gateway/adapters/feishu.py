"""Feishu bot adapter — WebSocket long connection + HTTP reply.

Uses websocket-client for raw transport, with optional lark-oapi protobuf
support for decoding binary frames sent by Feishu's WS gateway.
"""

import json
import logging
import os
import re
import time
import uuid
import urllib.request

from threading import Thread, Event
from typing import Callable

import websocket  # websocket-client

from ..base import AdapterConfig, BaseAdapter, MessageEvent

# ── Optional protobuf support (from lark-oapi) ──
try:
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame as _PbFrame
    from lark_oapi.ws.const import HEADER_TYPE
    from lark_oapi.ws.enum import MessageType as _PbMsgType
    _HAS_PROTOBUF = True
except ImportError:
    _PbFrame = None  # type: ignore
    _PbMsgType = None  # type: ignore
    _HAS_PROTOBUF = False

logger = logging.getLogger("shiyi.gateway.feishu")

# ──────────────────────────────────────────────
# Feishu API constants
# ──────────────────────────────────────────────

FEISHU_BASE = "https://open.feishu.cn"
TOKEN_URL = f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"
WS_ENDPOINT_URL = f"{FEISHU_BASE}/callback/ws/endpoint"
REPLY_URL = f"{FEISHU_BASE}/open-apis/im/v1/messages/{{msg_id}}/reply"
SEND_URL = f"{FEISHU_BASE}/open-apis/im/v1/messages"

# Feishu text message max length (approx)
MAX_TEXT_LENGTH = 4000


class FeishuAdapter(BaseAdapter):
    """Handles Feishu bot WebSocket connection and message routing."""

    # Max number of recently processed message IDs to track for dedup
    MAX_DEDUP_IDS = 200

    def __init__(self, config: AdapterConfig):
        super().__init__(config)
        self._thread: Thread | None = None
        self._stop_event = Event()
        self._ws: websocket.WebSocketApp | None = None
        self._tenant_token: str = ""
        self._token_expires_at: float = 0.0
        self._processed_ids: set = set()  # dedup already-processed message IDs
        self._dedup_file = os.path.expanduser("~/.shiyi/gateway_dedup.json")
        self._last_msg_id: dict = {}  # {conversation_id: msg_id} — for reply threading
        self._load_dedup()  # restore from previous runs

    def _load_dedup(self) -> None:
        """Restore dedup set from disk (survives restarts + WS reconnects)."""
        try:
            with open(self._dedup_file) as f:
                ids = json.load(f)
            self._processed_ids = set(ids[-self.MAX_DEDUP_IDS:])
            logger.debug("Dedup loaded: %d IDs", len(self._processed_ids))
        except (FileNotFoundError, json.JSONDecodeError):
            self._processed_ids = set()

    def _save_dedup(self) -> None:
        """Persist dedup set to disk."""
        try:
            os.makedirs(os.path.dirname(self._dedup_file), exist_ok=True)
            with open(self._dedup_file, 'w') as f:
                json.dump(list(self._processed_ids)[-self.MAX_DEDUP_IDS:], f)
        except Exception:
            logger.debug("Failed to save dedup file", exc_info=True)

    # ── HTTP helpers ──────────────────────────

    def _http_post(self, url: str, body: dict) -> dict:
        """Minimal JSON POST without aiohttp."""
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._tenant_token}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode("utf-8"))

    def _refresh_token(self) -> None:
        """Get/renew tenant_access_token."""
        if time.time() < self._token_expires_at - 60:  # 60s buffer
            return

        data = json.dumps(
            {
                "app_id": self.config.app_id,
                "app_secret": self.config.app_secret,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL, data=data, headers={"Content-Type": "application/json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))

        code = resp.get("code", -1)
        if code != 0:
            raise RuntimeError(f"Feishu token error code={code}: {resp.get('msg', '')}")

        self._tenant_token = resp["tenant_access_token"]
        self._token_expires_at = time.time() + resp.get("expire", 3600)
        logger.info("Feishu tenant token refreshed, expires in %ss", resp.get("expire", 0))

    def _get_ws_url(self) -> str:
        """Get the dynamic WebSocket URL from Feishu."""
        data = json.dumps({
            "AppID": self.config.app_id,
            "AppSecret": self.config.app_secret,
        }).encode("utf-8")
        req = urllib.request.Request(
            WS_ENDPOINT_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        code = resp.get("code", -1)
        if code != 0:
            raise RuntimeError(f"Feishu WS endpoint error code={code}: {resp.get('msg', '')}")
        ws_url = resp.get("data", {}).get("URL", "")
        if not ws_url:
            raise RuntimeError("Feishu WS endpoint returned empty URL")
        logger.info("Feishu WS endpoint: %s", ws_url[:80])
        return ws_url

    # ── Platform API ──────────────────────────

    # Map file extensions to Feishu file_type
    _FILE_TYPE_MAP = {
        'txt': 'stream', 'csv': 'stream', 'json': 'stream', 'log': 'stream',
        'md': 'stream',
        'pdf': 'pdf', 'doc': 'doc', 'docx': 'doc',
        'xls': 'xls', 'xlsx': 'xlsx', 'ppt': 'ppt', 'pptx': 'ppt',
        'png': 'image', 'jpg': 'image', 'jpeg': 'image', 'gif': 'image',
        'opus': 'opus', 'mp3': 'opus', 'wav': 'opus',
        'mp4': 'mp4',
    }

    def upload_file(self, file_path: str) -> str | None:
        """Upload a file to Feishu IM. Returns file_key or None on failure."""
        if not os.path.isfile(file_path):
            logger.error("File not found: %s", file_path)
            return None

        self._refresh_token()

        filename = os.path.basename(file_path)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        file_type = self._FILE_TYPE_MAP.get(ext, 'stream')

        with open(file_path, 'rb') as f:
            file_content = f.read()

        boundary = f"----FormBoundary{uuid.uuid4().hex[:16]}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file_type"\r\n\r\n'
            f"{file_type}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file_name"\r\n\r\n'
            f"{filename}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode('utf-8') + file_content + f"\r\n--{boundary}--\r\n".encode('utf-8')

        req = urllib.request.Request(
            f"{FEISHU_BASE}/open-apis/im/v1/files",
            data=body,
            headers={
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Authorization': f'Bearer {self._tenant_token}',
            },
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read().decode('utf-8'))
        code = resp.get('code', -1)
        if code != 0:
            logger.error("Feishu file upload failed: code=%s msg=%s", code, resp.get('msg', ''))
            return None

        file_key = resp['data']['file_key']
        logger.info("Feishu file uploaded: %s → %s", filename, file_key)
        return file_key

    def send_file(self, conversation_id: str, file_key: str, filename: str) -> None:
        """Send a file message to a Feishu user."""
        self._refresh_token()
        body = {
            "receive_id": conversation_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key}),
        }
        self._http_post(f"{SEND_URL}?receive_id_type=open_id", body)

    def reply(self, msg_id: str, text: str) -> dict | None:
        """Reply to a specific message (threaded). Returns API response or None."""
        self._refresh_token()
        url = REPLY_URL.format(msg_id=msg_id)
        # Chunk if too long
        chunk = text[:MAX_TEXT_LENGTH]
        if len(text) > MAX_TEXT_LENGTH:
            chunk += "…"
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }
        try:
            return self._http_post(url, body)
        except Exception as e:
            logger.error("Feishu reply failed: %s", e)
            return None

    def send(self, conversation_id: str, text: str) -> None:
        """Send a text reply to a Feishu user.

        If we have the last message_id for this conversation, use reply API
        for proper threading. Otherwise fall back to send API.
        """
        # Prefer reply (threaded) over send (new message)
        last_msg_id = self._last_msg_id.get(conversation_id, "")
        if last_msg_id:
            result = self.reply(last_msg_id, text)
            if result is not None:
                return
            # Reply failed, fall through to send

        # Fallback: send as new message
        self._refresh_token()
        chunk = text[:MAX_TEXT_LENGTH]
        if len(text) > MAX_TEXT_LENGTH:
            chunk += "…"
        body = {
            "receive_id": conversation_id,
            "msg_type": "text",
            "content": json.dumps({"text": chunk}),
        }
        try:
            self._http_post(f"{SEND_URL}?receive_id_type=open_id", body)
        except Exception as e:
            logger.error("Feishu send failed: %s", e)

    def send_long(self, conversation_id: str, text: str) -> None:
        """Send potentially long text, chunking if necessary."""
        if len(text) <= MAX_TEXT_LENGTH:
            self.send(conversation_id, text)
            return

        # Split into chunks at paragraph boundaries
        chunks = []
        remaining = text
        while remaining:
            if len(remaining) <= MAX_TEXT_LENGTH:
                chunks.append(remaining)
                break
            # Try to split at paragraph break
            split_at = remaining.rfind('\n\n', 0, MAX_TEXT_LENGTH)
            if split_at < MAX_TEXT_LENGTH // 2:
                split_at = remaining.rfind('\n', 0, MAX_TEXT_LENGTH)
            if split_at < MAX_TEXT_LENGTH // 2:
                split_at = MAX_TEXT_LENGTH
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:].lstrip('\n')

        for i, chunk in enumerate(chunks):
            if i > 0:
                time.sleep(0.3)  # Rate limit between chunks
            self.send(conversation_id, chunk)

    # ── WebSocket event handling ──────────────

    def _decode_pb_frame(self, data: bytes) -> str | None:
        """Decode a protobuf Frame from Feishu WS binary message."""
        frame = _PbFrame()
        frame.ParseFromString(data)

        headers = {h.key: h.value for h in frame.headers}
        msg_type = headers.get(HEADER_TYPE, "")

        logger.info("WS frame: type=%s method=%s service=%s", msg_type, frame.method, frame.service)

        if msg_type == _PbMsgType.PING.value:
            pong = _PbFrame()
            pong_header = pong.headers.add()
            pong_header.key = HEADER_TYPE
            pong_header.value = _PbMsgType.PONG.value
            pong.service = frame.service
            pong.method = 5  # CONTROL
            pong.SeqID = 0
            pong.LogID = 0
            if self._ws:
                self._ws.send(pong.SerializeToString(), opcode=websocket.ABNF.OPCODE_BINARY)
            return None

        if msg_type == _PbMsgType.EVENT.value:
            event_json = frame.payload.decode("utf-8")
            return event_json

        logger.debug("Feishu WS unknown msg_type=%s", msg_type)
        return None

    def _on_ws_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("Feishu WebSocket connected")
        self._load_dedup()   # restore dedup set from disk after reconnect

    def _on_ws_data(self, ws: websocket.WebSocketApp, data: bytes, opcode: int, flags: int) -> None:
        """Handle all WebSocket frames (text + binary)."""
        if opcode == websocket.ABNF.OPCODE_TEXT:
            raw_msg = data.decode("utf-8")
        elif opcode == websocket.ABNF.OPCODE_BINARY:
            if _HAS_PROTOBUF:
                raw_msg = self._decode_pb_frame(data)
                if not raw_msg:
                    return
            else:
                logger.warning("Feishu binary frame received but lark-oapi not installed (len=%d)", len(data))
                return
        else:
            return
        try:
            event = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("Feishu WS non-JSON message: %.200s", raw_msg)
            return

        # Ping → pong (JSON protocol fallback)
        if event.get("type") == "ping":
            ws.send(json.dumps({"type": "pong"}))
            return

        # Only handle text messages from users
        header = event.get("header", {})
        event_type = header.get("event_type", "")

        logger.info("WS event: type=%s full=%.500s", event_type, raw_msg)
        if event_type != "im.message.receive_v1":
            return

        event_data = event.get("event", {})
        message = event_data.get("message", {})
        msg_type = message.get("message_type", "text")
        msg_id = message.get("message_id", "")

        chat_type = message.get("chat_type", "")
        chat_id = message.get("chat_id", "")

        # Extract sender
        sender = event_data.get("sender", {}).get("sender_id", {})
        open_id = sender.get("open_id", "")

        # Determine conversation_id based on chat type
        if chat_type == "p2p":
            conversation_id = open_id
        elif chat_type == "group":
            # In group chats, use chat_id as conversation_id
            conversation_id = chat_id
            # Check if bot is @mentioned
            mentions = message.get("mentions", [])
            if not mentions:
                return  # In group, only respond when @mentioned
            # Check if our bot is among the mentions
            bot_mentioned = False
            for m in mentions:
                if m.get("id", {}).get("open_id") == self.config.app_id or \
                   m.get("id", {}).get("user_id") == self.config.app_id:
                    bot_mentioned = True
                    break
            if not bot_mentioned:
                # Our bot is not mentioned, check if any mention exists with our open_id
                # (Feishu puts bot's open_id in mentions when @bot)
                pass  # Some Feishu apps may not have app_id in mentions; accept for now
        else:
            return  # Unknown chat type

        # Extract text content based on message type
        text = ""
        content_str = message.get("content", "{}")

        if msg_type == "text":
            try:
                content = json.loads(content_str)
                text = content.get("text", "")
            except json.JSONDecodeError:
                text = content_str
        elif msg_type == "post":
            # Rich text / post message — extract plain text
            try:
                content = json.loads(content_str)
                text = self._extract_post_text(content)
            except json.JSONDecodeError:
                text = ""
        elif msg_type == "image":
            text = "[图片]"  # Acknowledge image receipt
        elif msg_type == "file":
            text = "[文件]"  # Acknowledge file receipt
        elif msg_type == "audio":
            text = "[语音]"
        else:
            # Unsupported type — skip silently
            logger.debug("Skipping unsupported message_type: %s", msg_type)
            return

        if not text.strip():
            return

        # For group messages, strip @mention prefix
        if chat_type == "group":
            # Remove @bot mention text like "@史佚 " from the beginning
            text = re.sub(r'^@\S+\s*', '', text).strip()
            if not text:
                return

        # Store msg_id for reply threading
        if msg_id and conversation_id:
            self._last_msg_id[conversation_id] = msg_id

        msg_event = MessageEvent(
            platform="feishu",
            user_id=open_id,
            conversation_id=conversation_id,
            content=text,
            raw=event,
        )

        # Dedup: skip messages already processed
        if msg_id and msg_id in self._processed_ids:
            logger.debug("Skipping duplicate message: id=%s", msg_id)
            return
        if msg_id:
            self._processed_ids.add(msg_id)
            if len(self._processed_ids) > self.MAX_DEDUP_IDS:
                self._processed_ids = set(list(self._processed_ids)[-self.MAX_DEDUP_IDS // 2:])
            self._save_dedup()

        # Callback to main loop
        if self._on_message_cb:
            self._on_message_cb(msg_event)

    @staticmethod
    def _extract_post_text(content: dict) -> str:
        """Extract plain text from a Feishu post (rich text) message."""
        title = content.get("title", "")
        parts = []
        if title:
            parts.append(title)
        for line in content.get("content", []):
            line_parts = []
            for elem in line:
                tag = elem.get("tag", "")
                if tag == "text":
                    line_parts.append(elem.get("text", ""))
                elif tag == "at":
                    line_parts.append(elem.get("user_name", "@用户"))
                elif tag == "a":
                    line_parts.append(elem.get("href", ""))
                elif tag == "code":
                    line_parts.append(elem.get("text", ""))
            parts.append("".join(line_parts))
        return "\n".join(parts)

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("Feishu WebSocket error: %s", error)

    def _on_ws_close(self, ws: websocket.WebSocketApp, code: int, msg: str) -> None:
        logger.info("Feishu WebSocket closed: code=%s msg=%s", code, msg)

    # ── Lifecycle ─────────────────────────────

    def start(self, on_message: Callable[[MessageEvent], None]) -> None:
        """Connect to Feishu bot WebSocket (blocking — run in thread)."""
        self._on_message_cb = on_message

        def _run():
            while not self._stop_event.is_set():
                try:
                    ws_url = self._get_ws_url()

                    self._ws = websocket.WebSocketApp(
                        ws_url,
                        on_open=self._on_ws_open,
                        on_data=self._on_ws_data,
                        on_error=self._on_ws_error,
                        on_close=self._on_ws_close,
                    )
                    # run_forever blocks; long intervals to survive LLM response delays
                    self._ws.run_forever(ping_interval=180, ping_timeout=120)
                except Exception:
                    logger.exception("Feishu WS loop error, retrying in 5s")
                if not self._stop_event.is_set():
                    self._stop_event.wait(5)  # backoff before reconnect

        self._thread = Thread(target=_run, daemon=True, name="feishu-gateway")
        self._thread.start()
        logger.info("Feishu adapter started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=5)
        # Save dedup state on exit (but do NOT delete it)
        self._save_dedup()
        logger.info("Feishu adapter stopped")
