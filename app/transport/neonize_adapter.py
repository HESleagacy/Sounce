from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.messages import OutboundMessage
from app.transport.base import MessageHandler
from app.transport.normalizer import jid_to_string, normalize_neonize_message

log = logging.getLogger(__name__)

RAW_EVENT_CACHE_SIZE = 256


class NeonizeAdapter:
    def __init__(self, session_path: Path, owner_jid: str, diagnostics: bool = False) -> None:
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, MessageEv, PairStatusEv
        except ImportError as exc:
            raise RuntimeError(f"Unable to load Neonize: {exc}") from exc

        session_path.parent.mkdir(parents=True, exist_ok=True)
        self._owner_jid = owner_jid
        self._diagnostics = diagnostics
        self._handler: MessageHandler | None = None
        self._connected = False
        self._raw_messages: OrderedDict[str, Any] = OrderedDict()
        self._client = NewClient(str(session_path))

        @self._client.event(PairStatusEv)
        def on_pair_status(_client: Any, event: Any) -> None:
            log.info("WhatsApp pair status: %s", event)

        @self._client.event(ConnectedEv)
        def on_connected(_client: Any, _event: Any) -> None:
            self._connected = True
            log.info("Connected to WhatsApp")

        @self._client.event(MessageEv)
        def on_message(_client: Any, event: Any) -> None:
            info = getattr(event, "Info", None)
            source = getattr(info, "MessageSource", None)
            if self._diagnostics:
                raw_message = getattr(event, "Message", None)
                fields = [field.name for field, _value in raw_message.ListFields()] if raw_message else []
                log.info(
                    "Raw message event: id=%s chat=%s sender=%s sender_alt=%s from_me=%s fields=%s",
                    getattr(info, "ID", None),
                    jid_to_string(getattr(source, "Chat", None)),
                    jid_to_string(getattr(source, "Sender", None)),
                    jid_to_string(getattr(source, "SenderAlt", None)),
                    getattr(source, "IsFromMe", None),
                    fields,
                )
            message_id = getattr(info, "ID", None)
            raw_message = getattr(event, "Message", None)
            if message_id and raw_message is not None:
                self._raw_messages[str(message_id)] = raw_message
                while len(self._raw_messages) > RAW_EVENT_CACHE_SIZE:
                    self._raw_messages.popitem(last=False)
            normalized = normalize_neonize_message(event, self._owner_jid)
            if normalized is not None and self._handler is not None:
                self._handler(normalized)

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def send_text(self, chat_jid: str, text: str) -> OutboundMessage:
        response = self._client.send_message(self._build_jid(chat_jid), text)
        return self._to_outbound(response, chat_jid, text)

    def send_voice_note(self, chat_jid: str, audio: bytes) -> OutboundMessage:
        message = self._client.build_audio_message(audio, ptt=True)
        message.audioMessage.mimetype = "audio/ogg; codecs=opus"
        response = self._client.send_message(self._build_jid(chat_jid), message)
        return self._to_outbound(response, chat_jid, "[voice note]")

    def download_media(self, whatsapp_message_id: str) -> bytes | None:
        raw_message = self._raw_messages.get(whatsapp_message_id)
        if raw_message is None:
            log.warning("No cached raw message for %s; cannot download media", whatsapp_message_id)
            return None
        try:
            return self._client.download_any(raw_message)
        except Exception:
            log.exception("Failed to download media for %s", whatsapp_message_id)
            return None

    def connect(self) -> None:
        try:
            self._client.connect()
        finally:
            # connect() blocks for the life of the session, so reaching here at
            # all means the link is down.
            self._connected = False

    def disconnect(self) -> None:
        self._connected = False
        self._client.disconnect()

    def is_connected(self) -> bool:
        return self._connected

    @staticmethod
    def _build_jid(chat_jid: str) -> Any:
        from neonize.utils.jid import build_jid

        user, _, server = chat_jid.partition("@")
        if not user or not server:
            raise ValueError(f"Invalid chat JID: {chat_jid!r}")
        return build_jid(user, server)

    @staticmethod
    def _to_outbound(response: Any, chat_jid: str, text: str) -> OutboundMessage:
        message_id = _extract_message_id(response)
        if not message_id:
            raise RuntimeError("Neonize sent a message but did not return its WhatsApp message ID")
        return OutboundMessage(
            whatsapp_message_id=message_id,
            chat_jid=chat_jid,
            text=text,
            occurred_at=datetime.now(timezone.utc),
        )


def _extract_message_id(response: Any) -> str | None:
    for candidate in (response, getattr(response, "Message", None), getattr(response, "Info", None)):
        if candidate is None:
            continue
        for name in ("ID", "Id", "id"):
            value = getattr(candidate, name, None)
            if value:
                return str(value)
    return None
