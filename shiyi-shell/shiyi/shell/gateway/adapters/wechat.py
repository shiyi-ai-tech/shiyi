"""WeChat personal account adapter — iLink Bot API + long-polling.

Connects to WeChat personal accounts via Tencent's iLink Bot API.
Supports QR code scanning login, private chat, and optional group chat.

Architecture (adapted for Shiyi sync):
- Inbound: long-poll ``getupdates`` in a background thread
- Outbound: ``sendmessage`` with ``context_token`` for session continuity
- Login: interactive QR code flow (one-time, uses asyncio internally)
- Media: AES-128-ECB encrypted CDN protocol (requires ``cryptography``)
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import struct
import textwrap
import time
import uuid
import urllib.request
import urllib.parse
from pathlib import Path
from threading import Thread, Event
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import AdapterConfig, BaseAdapter, MessageEvent

logger = logging.getLogger("shiyi.gateway.wechat")

# ── iLink API constants ────────────────────────────

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
MAX_TEXT_LENGTH = 2000

# iLink message item types
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

# ── Optional cryptography for AES CDN ──────────────

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

# ── Optional certifi for SSL ───────────────────────

try:
    import certifi
    import ssl
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except (ImportError, Exception):
    _SSL_CTX = None


# ── Low-level helpers (protocol layer) ──────────────

def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={urllib.parse.quote(upload_param, safe='')}"
        f"&filekey={urllib.parse.quote(filekey, safe='')}"
    )


def _is_stale_session_ret(
    ret: Optional[int], errcode: Optional[int], errmsg: Optional[str],
) -> bool:
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    return raw[:keep] if len(raw) > keep else raw


# ── AES helpers ────────────────────────────────────────────────────

def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


# ── Sync HTTP helpers (Shiyi architecture) ─────────

def _http_post(
    url: str,
    payload: Dict[str, Any],
    token: Optional[str] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Synchronous JSON POST to iLink API."""
    body = _json_dumps({**payload, "base_info": _base_info()})
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=_headers(token, body),
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"iLink POST HTTP {e.code}: {raw}") from e


def _http_get(url: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Synchronous JSON GET from iLink API."""
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"iLink GET HTTP {e.code}: {raw}") from e


# ── iLink API wrappers (protocol layer) ───────

def _get_updates(
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> Dict[str, Any]:
    """Long-poll for new messages. Returns response dict."""
    url = f"{base_url.rstrip('/')}/{EP_GET_UPDATES}"
    try:
        return _http_post(
            url,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout=timeout_ms / 1000 + 5,  # HTTP timeout > poll timeout
        )
    except Exception as exc:
        if "timed out" in str(exc).lower():
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
        raise


def _send_message_api(
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str] = None,
    client_id: str = "",
) -> Dict[str, Any]:
    """Send a text message via iLink sendmessage API."""
    if not text or not text.strip():
        raise ValueError("text must not be empty")
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    url = f"{base_url.rstrip('/')}/{EP_SEND_MESSAGE}"
    return _http_post(url, payload={"msg": message}, token=token, timeout=API_TIMEOUT_MS / 1000)


def _send_typing_api(
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    url = f"{base_url.rstrip('/')}/{EP_SEND_TYPING}"
    _http_post(
        url,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout=CONFIG_TIMEOUT_MS / 1000,
    )


def _get_config_api(
    base_url: str,
    token: str,
    user_id: str,
    context_token: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    url = f"{base_url.rstrip('/')}/{EP_GET_CONFIG}"
    return _http_post(url, payload=payload, token=token, timeout=CONFIG_TIMEOUT_MS / 1000)


# ── Message parsing ───────────────────────────────────────────────

def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    """Extract text content from iLink item_list."""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if ref_item:
                ref_type = ref_item.get("type")
                if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                    title = ref.get("title") or ""
                    prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                    return f"{prefix}{text}".strip()
                parts = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    # Fallback: check for voice transcription
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def _guess_chat_type(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    """Determine if message is from DM or group. Returns (chat_type, effective_chat_id)."""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (
        to_user_id and account_id and to_user_id != account_id
        and message.get("msg_type") == 1
    )
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


# ── Text chunking ─────────────────────────────────────────

def _split_text_chunks(text: str, max_len: int = MAX_TEXT_LENGTH) -> List[str]:
    """Split text into chunks at paragraph/line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try paragraph break
        cut = remaining.rfind('\n\n', 0, max_len)
        if cut < max_len // 2:
            cut = remaining.rfind('\n', 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip('\n')
    return chunks


# ── Persistent state helpers ────────────────────────

_SHIYI_HOME = Path.home() / ".shiyi"


def _account_dir() -> Path:
    path = _SHIYI_HOME / "weixin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(account_id: str) -> Path:
    return _account_dir() / f"{account_id}.json"


def save_weixin_account(*, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    """Persist iLink account credentials."""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(account_id)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(account_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted iLink account credentials."""
    path = _account_file(account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _sync_buf_path(account_id: str) -> Path:
    return _account_dir() / f"{account_id}.sync.json"


def _load_sync_buf(account_id: str) -> str:
    path = _sync_buf_path(account_id)
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
    except Exception:
        return ""


def _save_sync_buf(account_id: str, sync_buf: str) -> None:
    path = _sync_buf_path(account_id)
    path.write_text(json.dumps({"get_updates_buf": sync_buf}), encoding="utf-8")


# ── QR Login (interactive, one-time) ────────────────

def qr_login(timeout_seconds: int = 480) -> Optional[Dict[str, str]]:
    """Run the interactive iLink QR login flow.

    Displays QR code for the user to scan with WeChat.
    Returns credential dict on success, None on failure.

    Uses asyncio internally for the polling loop, but presents
    a synchronous interface to callers.
    """
    import asyncio

    async def _run() -> Optional[Dict[str, str]]:
        try:
            qr_resp = _http_get(
                f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}?bot_type=3",
                timeout=QR_TIMEOUT_MS / 1000,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None

        qr_scan_data = qrcode_url if qrcode_url else qrcode_value

        print("\n请使用微信扫描以下二维码：")
        if qrcode_url:
            print(qrcode_url)
        try:
            import qrcode
            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception as _qr_exc:
            print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = _http_get(
                    f"{current_base_url}/{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout=QR_TIMEOUT_MS / 1000,
                )
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qr_resp = _http_get(
                        f"{ILINK_BASE_URL}/{EP_GET_BOT_QR}?bot_type=3",
                        timeout=QR_TIMEOUT_MS / 1000,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    qr_scan_data = qrcode_url if qrcode_url else qrcode_value
                    if qrcode_url:
                        print(qrcode_url)
                    try:
                        import qrcode as _qrcode
                        qr = _qrcode.QRCode()
                        qr.add_data(qr_scan_data)
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload incomplete")
                    return None
                save_weixin_account(
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                print(f"\n微信连接成功！account_id={account_id}")
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
            await asyncio.sleep(1)

        print("\n微信登录超时。")
        return None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop (e.g. FastAPI)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _run())
            return future.result(timeout=timeout_seconds + 30)
    else:
        return asyncio.run(_run())


# ── ContextTokenStore (sync, disk-backed) ───────────

class ContextTokenStore:
    """Disk-backed context_token cache keyed by account + peer."""

    def __init__(self):
        self._root = _account_dir()
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin: failed to restore context tokens for %s: %s",
                         _safe_id(account_id), exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info("weixin: restored %d context token(s) for %s", restored, _safe_id(account_id))

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            self._path(account_id).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("weixin: failed to persist context tokens for %s: %s",
                         _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing ticket cache."""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


# ════════════════════════════════════════════════════
# Main Adapter
# ════════════════════════════════════════════════════


class WeChatAdapter(BaseAdapter):
    """WeChat personal account adapter via Tencent iLink Bot API.

    Inbound: long-polling getupdates in a background thread.
    Outbound: sendmessage with context_token for session continuity.
    Login: QR code scanning (one-time setup).
    """

    MAX_DEDUP_IDS = 200
    MAX_TEXT_LENGTH = MAX_TEXT_LENGTH
    SEND_CHUNK_DELAY = 1.5
    SEND_CHUNK_RETRIES = 4
    SEND_CHUNK_RETRY_DELAY = 1.0

    def __init__(self, config: AdapterConfig):
        super().__init__(config)
        self._thread: Optional[Thread] = None
        self._stop_event = Event()
        self._on_message_cb: Optional[Callable] = None

        # iLink credentials (from config.extra or env)
        self._account_id = str(
            config.extra.get("account_id") or os.getenv("WEIXIN_ACCOUNT_ID", "")
        ).strip()
        self._token = str(
            config.extra.get("token") or os.getenv("WEIXIN_TOKEN", "")
        ).strip()
        self._base_url = str(
            config.extra.get("base_url") or os.getenv("WEIXIN_BASE_URL", ILINK_BASE_URL)
        ).strip().rstrip("/")
        self._cdn_base_url = str(
            config.extra.get("cdn_base_url") or os.getenv("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL)
        ).strip().rstrip("/")

        # DM/Group policy
        self._dm_policy = str(
            config.extra.get("dm_policy") or os.getenv("WEIXIN_DM_POLICY", "open")
        ).strip().lower()
        self._group_policy = str(
            config.extra.get("group_policy") or os.getenv("WEIXIN_GROUP_POLICY", "disabled")
        ).strip().lower()
        self._allow_from = self._coerce_list(
            config.extra.get("allow_from") or os.getenv("WEIXIN_ALLOWED_USERS", "")
        )
        self._group_allow_from = self._coerce_list(
            config.extra.get("group_allow_from") or os.getenv("WEIXIN_GROUP_ALLOWED_USERS", "")
        )

        # Context tokens
        self._token_store = ContextTokenStore()

        # Typing ticket cache
        self._typing_cache = TypingTicketCache()

        # Poll state
        self._sync_buf = ""

        # Dedup
        self._processed_ids: set = set()
        self._dedup_file = os.path.expanduser("~/.shiyi/gateway_wechat_dedup.json")
        self._load_dedup()

        # Try to load saved credentials if token not explicitly set
        if self._account_id and not self._token:
            persisted = load_weixin_account(self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = str(
                    persisted.get("base_url") or self._base_url
                ).strip().rstrip("/")

    @staticmethod
    def _coerce_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    # ── Dedup (same pattern as feishu.py) ──────────

    def _load_dedup(self) -> None:
        try:
            with open(self._dedup_file) as f:
                ids = json.load(f)
            self._processed_ids = set(ids[-self.MAX_DEDUP_IDS:])
        except (FileNotFoundError, json.JSONDecodeError):
            self._processed_ids = set()

    def _save_dedup(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._dedup_file), exist_ok=True)
            with open(self._dedup_file, 'w') as f:
                json.dump(list(self._processed_ids)[-self.MAX_DEDUP_IDS:], f)
        except Exception:
            logger.debug("Failed to save dedup file", exc_info=True)

    # ── Adapter Interface ───────────────────────────

    def start(self, on_message: Callable[[MessageEvent], None]) -> None:
        """Start the WeChat adapter — long-polling in background thread."""
        if not self._token:
            raise RuntimeError(
                "WeChat iLink token missing. Run 'shiyi wechat-login' to set up, "
                "or set WEIXIN_TOKEN env var."
            )
        if not self._account_id:
            raise RuntimeError(
                "WeChat account_id missing. Set WEIXIN_ACCOUNT_ID env var "
                "or configure wechat.extra.account_id."
            )

        self._on_message_cb = on_message
        self._token_store.restore(self._account_id)
        self._sync_buf = _load_sync_buf(self._account_id)

        self._thread = Thread(target=self._poll_loop, daemon=True, name="wechat-gateway")
        self._thread.start()
        logger.info("WeChat adapter started (account=%s, base=%s)",
                     _safe_id(self._account_id), self._base_url)

    def stop(self) -> None:
        """Gracefully shut down the adapter."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._save_dedup()
        if self._sync_buf:
            _save_sync_buf(self._account_id, self._sync_buf)
        logger.info("WeChat adapter stopped")

    def send(self, conversation_id: str, text: str) -> None:
        """Send a text reply via iLink sendmessage API."""
        context_token = self._token_store.get(self._account_id, conversation_id)
        chunks = _split_text_chunks(text, self.MAX_TEXT_LENGTH)

        for idx, chunk in enumerate(chunks):
            client_id = f"shiyi-weixin-{uuid.uuid4().hex}"
            self._send_text_chunk(conversation_id, chunk, context_token, client_id)
            if idx < len(chunks) - 1 and self.SEND_CHUNK_DELAY > 0:
                time.sleep(self.SEND_CHUNK_DELAY)

    def send_long(self, conversation_id: str, text: str) -> None:
        """Send potentially long text, auto-chunked."""
        self.send(conversation_id, text)

    def _send_text_chunk(
        self,
        chat_id: str,
        chunk: str,
        context_token: Optional[str],
        client_id: str,
    ) -> None:
        """Send a single text chunk with retry and session-expired fallback."""
        retried_without_token = False
        last_error: Optional[Exception] = None

        for attempt in range(self.SEND_CHUNK_RETRIES + 1):
            try:
                resp = _send_message_api(
                    self._base_url,
                    self._token,
                    to=chat_id,
                    text=chunk,
                    context_token=context_token,
                    client_id=client_id,
                )
                # Check for iLink error codes
                if isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if (ret is not None and ret not in {0}) or (errcode is not None and errcode not in {0}):
                        is_session_expired = (
                            ret == SESSION_EXPIRED_ERRCODE
                            or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                        )
                        if is_session_expired and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            logger.warning(
                                "session expired for %s; retrying without context_token",
                                _safe_id(chat_id),
                            )
                            continue
                        is_rate_limited = (
                            ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE
                        )
                        if is_rate_limited:
                            wait = self.SEND_CHUNK_RETRY_DELAY * 3
                            logger.warning("rate limited for %s; backing off %.1fs",
                                         _safe_id(chat_id), wait)
                            time.sleep(wait)
                            continue
                        errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
                        raise RuntimeError(
                            f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}"
                        )
                return  # success
            except Exception as exc:
                last_error = exc
                if attempt >= self.SEND_CHUNK_RETRIES:
                    break
                wait = self.SEND_CHUNK_RETRY_DELAY * (attempt + 1)
                logger.warning(
                    "send chunk failed to=%s attempt=%d/%d, retrying in %.1fs: %s",
                    _safe_id(chat_id), attempt + 1, self.SEND_CHUNK_RETRIES + 1, wait, exc,
                )
                if wait > 0:
                    time.sleep(wait)

        if last_error:
            logger.error("send chunk exhausted retries for %s: %s", _safe_id(chat_id), last_error)

    # ── Typing indicators ───────────────────────────

    def send_typing(self, chat_id: str) -> None:
        """Send typing indicator to user."""
        typing_ticket = self._typing_cache.get(chat_id)
        if not typing_ticket:
            return
        try:
            _send_typing_api(
                self._base_url, self._token,
                to_user_id=chat_id, typing_ticket=typing_ticket, status=TYPING_START,
            )
        except Exception as exc:
            logger.debug("typing start failed for %s: %s", _safe_id(chat_id), exc)

    def stop_typing(self, chat_id: str) -> None:
        """Stop typing indicator."""
        typing_ticket = self._typing_cache.get(chat_id)
        if not typing_ticket:
            return
        try:
            _send_typing_api(
                self._base_url, self._token,
                to_user_id=chat_id, typing_ticket=typing_ticket, status=TYPING_STOP,
            )
        except Exception as exc:
            logger.debug("typing stop failed for %s: %s", _safe_id(chat_id), exc)

    def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        """Fetch typing ticket from getconfig API (fire-and-forget)."""
        if self._typing_cache.get(user_id):
            return
        try:
            response = _get_config_api(
                self._base_url, self._token,
                user_id=user_id, context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:
            logger.debug("getConfig failed for %s: %s", _safe_id(user_id), exc)

    # ── DM/Group policy ─────────────────────────────

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "disabled":
            return False
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        return True

    # ── Long-poll loop ──────────────────────────────

    def _poll_loop(self) -> None:
        """Main poll loop running in background thread."""
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                response = _get_updates(
                    self._base_url,
                    self._token,
                    self._sync_buf,
                )

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)

                if ret not in {0, None} or errcode not in {0, None}:
                    if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, response.get("errmsg"))):
                        logger.error("Session expired; pausing for 10 minutes")
                        self._stop_event.wait(600)
                        consecutive_failures = 0
                        continue

                    consecutive_failures += 1
                    logger.warning(
                        "getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)",
                        ret, errcode, response.get("errmsg", ""),
                        consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    delay = (BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                             else RETRY_DELAY_SECONDS)
                    self._stop_event.wait(delay)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0

                # Update sync buffer
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    self._sync_buf = new_sync_buf
                    _save_sync_buf(self._account_id, self._sync_buf)

                # Process messages
                for message in response.get("msgs") or []:
                    try:
                        self._process_message(message)
                    except Exception as exc:
                        logger.error("inbound error from=%s: %s",
                                   _safe_id(message.get("from_user_id")), exc, exc_info=True)

            except Exception as exc:
                consecutive_failures += 1
                logger.error("poll error (%d/%d): %s",
                           consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                delay = (BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                         else RETRY_DELAY_SECONDS)
                self._stop_event.wait(delay)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    def _process_message(self, message: Dict[str, Any]) -> None:
        """Process a single inbound message from iLink."""
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id:
            return
        # Skip our own messages
        if sender_id == self._account_id:
            return

        # Message ID dedup
        message_id = str(message.get("message_id") or "").strip()
        if message_id and message_id in self._processed_ids:
            logger.debug("Dedup: skipping message %s", message_id)
            return
        if message_id:
            self._processed_ids.add(message_id)
            if len(self._processed_ids) > self.MAX_DEDUP_IDS:
                self._processed_ids = set(list(self._processed_ids)[-self.MAX_DEDUP_IDS // 2:])
            self._save_dedup()

        # Content-fingerprint dedup
        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if content_key in self._processed_ids:
                logger.debug("Content-dedup: skipping duplicate from %s", sender_id)
                return
            self._processed_ids.add(content_key)

        # Determine chat type
        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)

        # Apply access policy
        if chat_type == "group":
            if self._group_policy == "disabled":
                return
            if self._group_policy == "allowlist" and effective_chat_id not in self._group_allow_from:
                return
        elif not self._is_dm_allowed(sender_id):
            return

        # Store context_token for outbound replies
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)

        # Fetch typing ticket (fire-and-forget in background)
        typing_thread = Thread(
            target=self._maybe_fetch_typing_ticket,
            args=(sender_id, context_token or None),
            daemon=True,
        )
        typing_thread.start()

        # Handle non-text items
        if not text:
            for item in item_list:
                item_type = item.get("type")
                if item_type == ITEM_IMAGE:
                    text = "[图片]"
                elif item_type == ITEM_VOICE:
                    voice_text = str((item.get("voice_item") or {}).get("text") or "")
                    text = voice_text if voice_text else "[语音]"
                elif item_type == ITEM_FILE:
                    filename = str((item.get("file_item") or {}).get("file_name") or "")
                    text = f"[文件: {filename}]" if filename else "[文件]"
                elif item_type == ITEM_VIDEO:
                    text = "[视频]"
                if text:
                    break

        if not text or not text.strip():
            return

        # Build conversation_id
        if chat_type == "group":
            conversation_id = effective_chat_id
        else:
            conversation_id = sender_id

        msg_event = MessageEvent(
            platform="wechat",
            user_id=sender_id,
            conversation_id=conversation_id,
            content=text,
            raw=message,
        )

        if self._on_message_cb:
            self._on_message_cb(msg_event)

    # ── Media upload ─────────────────────────────────

    _FILE_TYPE_MAP = {
        'png': MEDIA_IMAGE, 'jpg': MEDIA_IMAGE, 'jpeg': MEDIA_IMAGE,
        'gif': MEDIA_IMAGE, 'webp': MEDIA_IMAGE,
        'mp4': MEDIA_VIDEO, 'mov': MEDIA_VIDEO, 'avi': MEDIA_VIDEO,
        'opus': MEDIA_VOICE, 'silk': MEDIA_VOICE,
    }

    def upload_file(self, file_path: str, to_user_id: str = "") -> Optional[str]:
        """Upload file to WeChat iLink CDN. Returns file_key or None."""
        if not _HAS_CRYPTO:
            logger.warning("WeChat file upload requires 'cryptography' package")
            return None
        if not os.path.isfile(file_path):
            logger.error("File not found: %s", file_path)
            return None

        plaintext = Path(file_path).read_bytes()
        ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''
        media_type = self._FILE_TYPE_MAP.get(ext, MEDIA_FILE)

        try:
            filekey = secrets.token_hex(16)
            aes_key = secrets.token_bytes(16)
            rawsize = len(plaintext)
            rawfilemd5 = hashlib.md5(plaintext).hexdigest()
            ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

            # Step 1: get upload URL
            upload_resp = self._get_upload_url(
                to_user_id=to_user_id,
                media_type=media_type,
                filekey=filekey,
                rawsize=rawsize,
                rawfilemd5=rawfilemd5,
                ciphertext_size=len(ciphertext),
                aeskey_hex=aes_key.hex(),
            )
            upload_param = str(upload_resp.get("upload_param") or "")
            upload_full_url = str(upload_resp.get("upload_full_url") or "")

            if not upload_param and not upload_full_url:
                logger.error("getUploadUrl returned no upload_param or upload_full_url")
                return None

            # Step 2: upload encrypted data to CDN
            upload_url = upload_full_url or _cdn_upload_url(self._cdn_base_url, upload_param, filekey)
            encrypted_query_param = self._upload_ciphertext(ciphertext, upload_url)

            # Step 3: build media item and return as JSON string (acts as file_key)
            aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
            media_item = self._build_media_item(
                media_type=media_type,
                ext=ext,
                encrypt_query_param=encrypted_query_param,
                aes_key_for_api=aes_key_for_api,
                ciphertext_size=len(ciphertext),
                plaintext_size=rawsize,
                filename=Path(file_path).name,
                rawfilemd5=rawfilemd5,
            )

            # Return JSON-encoded media item as the "file key"
            result = json.dumps({
                "media_item": media_item,
                "filekey": filekey,
            })
            logger.info("WeChat file uploaded: %s → %s", Path(file_path).name, filekey[:8])
            return result

        except Exception as exc:
            logger.error("WeChat file upload failed: %s", exc)
            return None

    def send_file(self, conversation_id: str, media_id: str, filename: str = "") -> None:
        """Send file via iLink API. media_id is JSON from upload_file()."""
        if not media_id:
            return
        try:
            data = json.loads(media_id)
            media_item = data.get("media_item", {})
        except (json.JSONDecodeError, AttributeError):
            logger.error("Invalid media_id format")
            return

        context_token = self._token_store.get(self._account_id, conversation_id)
        client_id = f"shiyi-weixin-{uuid.uuid4().hex}"

        try:
            url = f"{self._base_url}/{EP_SEND_MESSAGE}"
            msg = {
                "from_user_id": "",
                "to_user_id": conversation_id,
                "client_id": client_id,
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [media_item],
            }
            if context_token:
                msg["context_token"] = context_token
            _http_post(url, payload={"msg": msg}, token=self._token, timeout=API_TIMEOUT_MS / 1000)
            logger.info("WeChat file sent to %s", _safe_id(conversation_id))
        except Exception as exc:
            logger.error("WeChat send_file failed: %s", exc)

    def _get_upload_url(
        self,
        to_user_id: str,
        media_type: int,
        filekey: str,
        rawsize: int,
        rawfilemd5: str,
        ciphertext_size: int,
        aeskey_hex: str,
    ) -> Dict[str, Any]:
        """Call iLink getUploadUrl API."""
        url = f"{self._base_url}/{EP_GET_UPLOAD_URL}"
        return _http_post(url, payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": ciphertext_size,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        }, token=self._token, timeout=API_TIMEOUT_MS / 1000)

    def _upload_ciphertext(self, ciphertext: bytes, upload_url: str) -> str:
        """Upload encrypted data to CDN. Returns encrypted_query_param."""
        req = urllib.request.Request(
            upload_url,
            data=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=120, context=_SSL_CTX)
        encrypted_param = resp.headers.get("x-encrypted-param")
        if encrypted_param:
            return encrypted_param
        raise RuntimeError(f"CDN upload missing x-encrypted-param header")

    @staticmethod
    def _build_media_item(
        media_type: int,
        ext: str,
        encrypt_query_param: str,
        aes_key_for_api: str,
        ciphertext_size: int,
        plaintext_size: int,
        filename: str,
        rawfilemd5: str,
    ) -> Dict[str, Any]:
        """Build iLink media item dict for sendmessage."""
        media_common = {
            "encrypt_query_param": encrypt_query_param,
            "aes_key": aes_key_for_api,
            "encrypt_type": 1,
        }
        if media_type == MEDIA_IMAGE:
            return {
                "type": ITEM_IMAGE,
                "image_item": {"media": media_common, "mid_size": ciphertext_size},
            }
        if media_type == MEDIA_VIDEO:
            return {
                "type": ITEM_VIDEO,
                "video_item": {"media": media_common, "video_size": ciphertext_size, "video_md5": rawfilemd5},
            }
        if media_type == MEDIA_VOICE and ext == "silk":
            return {
                "type": ITEM_VOICE,
                "voice_item": {"media": media_common, "encode_type": 6, "sample_rate": 24000, "bits_per_sample": 16},
            }
        return {
            "type": ITEM_FILE,
            "file_item": {"media": media_common, "file_name": filename, "len": str(plaintext_size)},
        }
