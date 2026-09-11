from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.domain.messages import InboundMessage, OutboundMessage

MessageHandler = Callable[[InboundMessage], None]


class MessageTransport(Protocol):
    def set_message_handler(self, handler: MessageHandler) -> None: ...

    def send_text(self, chat_jid: str, text: str) -> OutboundMessage: ...

    def send_voice_note(self, chat_jid: str, audio: bytes) -> OutboundMessage: ...

    def download_media(self, whatsapp_message_id: str) -> bytes | None: ...

    def connect(self) -> None: ...

    def is_connected(self) -> bool: ...

    def disconnect(self) -> None: ...
