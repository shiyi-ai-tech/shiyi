"""Gateway configuration: env vars first, then ~/.shiyi/gateway.yaml."""

import os
from pathlib import Path

from .base import AdapterConfig

_GATEWAY_YAML = Path.home() / ".shiyi" / "gateway.yaml"


def load_feishu_config() -> AdapterConfig:
    """Build Feishu adapter config from environment or YAML.

    Reads directly from .env files (not os.environ) to avoid pollution
    from other processes (e.g. Hermes gateway) that set FEISHU_* env vars.
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
