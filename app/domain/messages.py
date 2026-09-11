from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    whatsapp_message_id: str
    chat_jid: str
    sender_jid: str
    message_type: MessageType
    text: str | None
    occurred_at: datetime
    is_from_me: bool
    is_self_chat: bool
    media_mime_type: str | None = None
    media_filename: str | None = None
    media_size: int | None = None


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    whatsapp_message_id: str
    chat_jid: str
    text: str
    occurred_at: datetime


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, str)):
        return int(value)
    return None


def inbound_to_payload(message: InboundMessage) -> dict[str, object]:
    """Serialize an inbound message so it can survive a restart in the database."""
    return {
        "whatsapp_message_id": message.whatsapp_message_id,
        "chat_jid": message.chat_jid,
        "sender_jid": message.sender_jid,
        "message_type": message.message_type.value,
        "text": message.text,
        "occurred_at": message.occurred_at.isoformat(),
        "is_from_me": message.is_from_me,
        "is_self_chat": message.is_self_chat,
        "media_mime_type": message.media_mime_type,
        "media_filename": message.media_filename,
        "media_size": message.media_size,
    }


def inbound_from_payload(payload: dict[str, object]) -> InboundMessage:
    occurred_at = datetime.fromisoformat(str(payload["occurred_at"]))
    return InboundMessage(
        whatsapp_message_id=str(payload["whatsapp_message_id"]),
        chat_jid=str(payload["chat_jid"]),
        sender_jid=str(payload["sender_jid"]),
        message_type=MessageType(str(payload["message_type"])),
        text=_optional_str(payload.get("text")),
        occurred_at=occurred_at,
        is_from_me=bool(payload["is_from_me"]),
        is_self_chat=bool(payload["is_self_chat"]),
        media_mime_type=_optional_str(payload.get("media_mime_type")),
        media_filename=_optional_str(payload.get("media_filename")),
        media_size=_optional_int(payload.get("media_size")),
    )
