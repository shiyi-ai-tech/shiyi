"""Gateway configuration: env vars first, then ~/.shiyi/gateway.yaml."""

import os
from pathlib import Path

from .base import AdapterConfig

_GATEWAY_YAML = Path.home() / ".shiyi" / "gateway.yaml"


def load_feishu_config() -> AdapterConfig:
    """Build Feishu adapter config from environment or YAML.

    Reads directly from .env files (not os.environ) to avoid pollution
    from other processes that set FEISHU_* env vars.
    """

    # Try YAML first
    if _GATEWAY_YAML.exists():
        import yaml

        raw = yaml.safe_load(_GATEWAY_YAML.read_text(encoding="utf-8"))
        feishu = raw.get("feishu", {})
        return AdapterConfig(
            app_id=feishu.get("app_id", ""),
            app_secret=feishu.get("app_secret", ""),
            verification_token=feishu.get("verification_token", ""),
            encrypt_key=feishu.get("encrypt_key", ""),
            extra=feishu.get("extra", {}),
        )

    # Fallback: read from .env files directly (avoid os.environ pollution)
    app_id = ""
    app_secret = ""
    env_files = [
        Path.home() / ".shiyi" / ".env",
        Path(__file__).parent.parent.parent.parent / ".env",  # project root
    ]
    for env_path in env_files:
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "FEISHU_APP_ID" and not app_id:
                    app_id = val
                elif key == "FEISHU_APP_SECRET" and not app_secret:
                    app_secret = val

    return AdapterConfig(
        app_id=app_id,
        app_secret=app_secret,
        verification_token=os.environ.get("FEISHU_VERIFICATION_TOKEN", ""),
        encrypt_key=os.environ.get("FEISHU_ENCRYPT_KEY", ""),
    )


def load_wechat_config() -> AdapterConfig:
    """Build WeChat adapter config from environment or YAML.

    WeChat personal account uses iLink Bot API (QR login + token),
    not 公众号 (app_id/app_secret/webhook).

    Reads from gateway.yaml or .env files.
    """
    # Try YAML first
    if _GATEWAY_YAML.exists():
        import yaml

        raw = yaml.safe_load(_GATEWAY_YAML.read_text(encoding="utf-8"))
        wechat = raw.get("wechat", {})
        if wechat:
            return AdapterConfig(
                app_id=wechat.get("app_id", ""),
                app_secret=wechat.get("app_secret", ""),
                verification_token=wechat.get("token", ""),
                encrypt_key=wechat.get("encoding_aes_key", ""),
                extra={
                    "account_id": wechat.get("account_id", ""),
                    "token": wechat.get("ilink_token", ""),
                    "base_url": wechat.get("base_url", ""),
                    "cdn_base_url": wechat.get("cdn_base_url", ""),
                    **wechat.get("extra", {}),
                },
            )

    # Fallback: read from .env files
    account_id = ""
    ilink_token = ""
    base_url = ""
    env_files = [
        Path.home() / ".shiyi" / ".env",
        Path(__file__).parent.parent.parent.parent / ".env",
    ]
    for env_path in env_files:
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "WEIXIN_ACCOUNT_ID" and not account_id:
                    account_id = val
                elif key == "WEIXIN_TOKEN" and not ilink_token:
                    ilink_token = val
                elif key == "WEIXIN_BASE_URL" and not base_url:
                    base_url = val

    # Try loading saved credentials from iLink QR login
    if account_id and not ilink_token:
        try:
            from .adapters.wechat import load_weixin_account
            persisted = load_weixin_account(account_id)
            if persisted:
                ilink_token = str(persisted.get("token") or "")
                if not base_url:
                    base_url = str(persisted.get("base_url") or "")
        except Exception:
            pass

    return AdapterConfig(
        extra={
            "account_id": account_id,
            "token": ilink_token,
            "base_url": base_url,
        },
    )
