from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.messages import InboundMessage, MessageType


def jid_to_string(value: Any) -> str:
    if value is None:
        return ""
    user = getattr(value, "User", None)
    server = getattr(value, "Server", None)
    if user and server:
        result = f"{user}@{server}"
    elif hasattr(value, "User") and hasattr(value, "Server"):
        return ""
    else:
        result = str(value)
    result = result.strip().lower()
    if result.endswith("@c.us"):
        result = result.removesuffix("@c.us") + "@s.whatsapp.net"
    return result


def _non_empty_attr(value: Any, *names: str) -> Any:
    for name in names:
        candidate = getattr(value, name, None)
        if candidate not in (None, "", 0):
            return candidate
    return None


def _has_field(message: Any, name: str) -> bool:
    try:
        return bool(message.HasField(name))
    except (AttributeError, ValueError):
        return getattr(message, name, None) is not None


def _unwrap_message(message: Any) -> Any:
    wrapper_names = (
        "ephemeralMessage",
        "viewOnceMessage",
        "viewOnceMessageV2",
        "documentWithCaptionMessage",
        "groupMentionedMessage",
        "deviceSentMessage",
        "editedMessage",
    )
    for _ in range(5):
        wrapper = next(
            (getattr(message, name) for name in wrapper_names if _has_field(message, name)),
            None,
        )
        inner = getattr(wrapper, "message", None)
        if inner is None:
            break
        message = inner
    return message


def normalize_neonize_message(event: Any, owner_jid: str) -> InboundMessage | None:
    info = getattr(event, "Info", None)
    source = getattr(info, "MessageSource", None)
    message = getattr(event, "Message", None)
    if info is None or source is None or message is None:
        return None

    chat_jid = jid_to_string(getattr(source, "Chat", None))
    sender_jid = jid_to_string(getattr(source, "Sender", None)) or chat_jid
    owner_jid = jid_to_string(owner_jid)
    is_from_me = bool(getattr(source, "IsFromMe", False))
    is_lid_self_chat = (
        is_from_me
        and chat_jid.endswith("@lid")
        and sender_jid == chat_jid
        and not bool(getattr(source, "IsGroup", False))
    )
    is_self_chat = chat_jid == owner_jid or is_lid_self_chat
    if not is_self_chat:
        return None
    sender_alt_jid = jid_to_string(getattr(source, "SenderAlt", None))
    if sender_jid != owner_jid and sender_alt_jid != owner_jid and not is_lid_self_chat:
        return None
    sender_jid = owner_jid

    message = _unwrap_message(message)
    text: str | None = None
    message_type = MessageType.UNKNOWN
    media_mime_type: str | None = None
    media_filename: str | None = None
    media_size: int | None = None
    conversation = getattr(message, "conversation", None)
    extended = getattr(message, "extendedTextMessage", None)
    image = getattr(message, "imageMessage", None)
    audio = getattr(message, "audioMessage", None)
    document = getattr(message, "documentMessage", None)
    if conversation:
        text, message_type = conversation, MessageType.TEXT
    elif _has_field(message, "extendedTextMessage") and getattr(extended, "text", None):
        text, message_type = getattr(extended, "text", None), MessageType.TEXT
    elif _has_field(message, "imageMessage"):
        text, message_type = getattr(image, "caption", None), MessageType.IMAGE
        media_mime_type = getattr(image, "mimetype", None)
        media_size = getattr(image, "fileLength", None)
    elif _has_field(message, "audioMessage"):
        message_type = MessageType.AUDIO
        media_mime_type = getattr(audio, "mimetype", None)
        media_size = getattr(audio, "fileLength", None)
    elif _has_field(message, "documentMessage"):
        text, message_type = getattr(document, "caption", None), MessageType.DOCUMENT
        media_mime_type = getattr(document, "mimetype", None)
        media_filename = getattr(document, "fileName", None)
        media_size = getattr(document, "fileLength", None)

    message_id = _non_empty_attr(info, "ID", "Id", "id")
    if message_id is None:
        return None
    timestamp = _non_empty_attr(info, "Timestamp", "timestamp")
    if isinstance(timestamp, datetime):
        occurred_at = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
    else:
        try:
            occurred_at = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            occurred_at = datetime.now(timezone.utc)

    return InboundMessage(
        whatsapp_message_id=str(message_id),
        chat_jid=chat_jid,
        sender_jid=sender_jid,
        message_type=message_type,
        text=text.strip() if text else None,
        occurred_at=occurred_at,
        is_from_me=is_from_me,
        is_self_chat=is_self_chat,
        media_mime_type=media_mime_type or None,
        media_filename=media_filename or None,
        media_size=int(media_size) if media_size else None,
    )
