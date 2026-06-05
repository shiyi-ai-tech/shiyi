"""Gateway base types and abstract adapter interface.

v0.18+: Gateway != Clerk — gateway is message transport, not tool execution.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ──────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────


@dataclass
class MessageEvent:
    """A single inbound message from an external platform."""

    platform: str  # 'feishu', 'wechat', etc.
    user_id: str  # platform-specific sender ID
    conversation_id: str  # unique per-user scope for history isolation
    content: str  # plain text of the message
    raw: dict[str, Any] = field(default_factory=dict)  # original platform event


@dataclass
class AdapterConfig:
    """Configuration required by a platform adapter."""

    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────
# Abstract adapter
# ──────────────────────────────────────────────


class BaseAdapter(ABC):
    """Each platform implements this to bridge external messages → Shiyi."""

    def __init__(self, config: AdapterConfig):
        self.config = config

    @abstractmethod
    def start(self, on_message: Callable[[MessageEvent], None]) -> None:
        """Launch the adapter's message loop (WebSocket / webhook / polling).

        on_message must be called for every incoming user message.
        """
        ...

    @abstractmethod
    def send(self, conversation_id: str, text: str) -> None:
        """Send a reply back to the platform."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Gracefully shut down the adapter."""
        ...
