from __future__ import annotations

from types import SimpleNamespace

from app.domain.messages import MessageType
from app.transport.normalizer import normalize_neonize_message


def event(
    *,
    chat: str = "919876543210@s.whatsapp.net",
    sender: str = "919876543210@s.whatsapp.net",
    message_id: str = "message-1",
    text: str = "Kal milte hain",
    is_from_me: bool = True,
) -> SimpleNamespace:
    source = SimpleNamespace(Chat=chat, Sender=sender, IsFromMe=is_from_me)
    info = SimpleNamespace(ID=message_id, Timestamp=1_700_000_000, MessageSource=source)
    message = SimpleNamespace(
        conversation=text,
        extendedTextMessage=None,
        imageMessage=None,
        audioMessage=None,
        documentMessage=None,
    )
    return SimpleNamespace(Info=info, Message=message)


def test_normalizes_owner_self_chat_even_when_from_me() -> None:
    normalized = normalize_neonize_message(event(), "919876543210@s.whatsapp.net")

    assert normalized is not None
    assert normalized.text == "Kal milte hain"
    assert normalized.message_type == MessageType.TEXT
    assert normalized.is_from_me is True
    assert normalized.is_self_chat is True


def test_rejects_non_owner_chat_and_sender() -> None:
    assert normalize_neonize_message(event(chat="120363@g.us"), "919876543210@s.whatsapp.net") is None
    assert (
        normalize_neonize_message(
            event(sender="911111111111@s.whatsapp.net", is_from_me=False), "919876543210@s.whatsapp.net"
        )
        is None
    )


def test_extracts_extended_text() -> None:
    raw = event(text="")
    raw.Message.extendedTextMessage = SimpleNamespace(text="  Haan  ")

    normalized = normalize_neonize_message(raw, "919876543210@s.whatsapp.net")

    assert normalized is not None
    assert normalized.text == "Haan"


def test_identifies_audio_without_misclassifying_it_as_image() -> None:
    raw = event(text="")
    raw.Message.audioMessage = SimpleNamespace()

    normalized = normalize_neonize_message(raw, "919876543210@s.whatsapp.net")

    assert normalized is not None
    assert normalized.message_type == MessageType.AUDIO


def test_unwraps_device_sent_self_chat_text() -> None:
    raw = event(text="")
    inner = event(text="hello").Message
    raw.Message.deviceSentMessage = SimpleNamespace(message=inner)

    normalized = normalize_neonize_message(raw, "919876543210@s.whatsapp.net")

    assert normalized is not None
    assert normalized.message_type == MessageType.TEXT
    assert normalized.text == "hello"


def test_accepts_linked_identity_self_chat_but_not_other_outgoing_chat() -> None:
    lid = "280409992609926@lid"
    self_chat = normalize_neonize_message(event(chat=lid, sender=lid), "919876543210@s.whatsapp.net")
    other_chat = normalize_neonize_message(
        event(chat="123456789@lid", sender=lid), "919876543210@s.whatsapp.net"
    )

    assert self_chat is not None
    assert self_chat.chat_jid == lid
    assert self_chat.sender_jid == "919876543210@s.whatsapp.net"
    assert other_chat is None
