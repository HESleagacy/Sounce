from __future__ import annotations

from datetime import datetime, timezone

import pytest
from app.assistant.context import build_context
from app.domain.messages import InboundMessage, MessageType
from app.persistence.database import Database
from app.persistence.models import Base
from app.persistence.repositories import Repository
from app.providers.web import SafeWebFetcher, first_url

OWNER = "919876543210@s.whatsapp.net"


def test_context_includes_memory_source_provenance(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        message = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id="source-1",
                chat_jid=OWNER,
                sender_jid=OWNER,
                message_type=MessageType.TEXT,
                text="Mujhe penicillin se allergy hai",
                occurred_at=datetime.now(timezone.utc),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        repository.add_memory(
            user.id, message.id, "personal_fact", "health", "User reports penicillin allergy", 1.0
        )
        context = build_context(repository, user, None)

    source = context.memories[0]["source"]
    assert source["message_id"] == "source-1"
    assert "penicillin" in source["text"]
    assert context.recent_messages[-1]["text"] == "Mujhe penicillin se allergy hai"


def test_first_url_extracts_http_links() -> None:
    assert first_url("Read https://example.com/page please") == "https://example.com/page"
    assert first_url("no link") is None


def test_web_fetcher_rejects_private_destinations() -> None:
    fetcher = SafeWebFetcher(max_bytes=1024)
    with pytest.raises(ValueError, match="Private or local"):
        fetcher.fetch("http://127.0.0.1/private")
