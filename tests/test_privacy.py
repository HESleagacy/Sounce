"""Export, erase, and retention."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.messages import InboundMessage, MessageType
from app.persistence.database import Database
from app.persistence.models import Base, Document, Memory, Message, Reminder, User
from app.persistence.repositories import Repository
from app.privacy import apply_retention, export_user_data, purge_user_data, write_export
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


def _message(message_id: str, text: str, when: datetime) -> InboundMessage:
    return InboundMessage(
        whatsapp_message_id=message_id,
        chat_jid=OWNER,
        sender_jid=OWNER,
        message_type=MessageType.TEXT,
        text=text,
        occurred_at=when,
        is_from_me=True,
        is_self_chat=True,
    )


def seed(tmp_path: Path) -> tuple[Database, int, Path]:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    database = Database(f"sqlite:///{tmp_path / 'privacy.db'}")
    Base.metadata.create_all(database.engine)
    now = datetime.now(timezone.utc)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        cited = repository.add_inbound(
            user, _message("cited", "Mera doctor Dr Sharma hai", now - timedelta(days=500))
        )
        chatter = repository.add_inbound(user, _message("chatter", "kaisa hai", now - timedelta(days=500)))
        recent = repository.add_inbound(user, _message("recent", "aaj ka message", now))
        chatter.media_path = str(media_dir / "old.ogg")
        (media_dir / "old.ogg").write_bytes(b"audio")
        (media_dir / "bill.pdf").write_bytes(b"pdf")
        repository.add_memory(user.id, cited.id, "personal_fact", "doctor", "Dr Sharma", 1.0)
        repository.add_reminder(
            user_id=user.id,
            source_message_id=recent.id,
            title="Call Ramesh",
            due_at=now + timedelta(days=1),
            timezone_name="Asia/Kolkata",
        )
        repository.add_document(
            user.id,
            chatter.id,
            "bill.pdf",
            "application/pdf",
            str(media_dir / "bill.pdf"),
            "Electricity bill",
            "Due soon",
        )
        session.flush()
        for record in session.scalars(select(Document)):
            record.created_at = now - timedelta(days=500)
        user_id = user.id
    return database, user_id, media_dir


def test_export_contains_every_stored_fact(tmp_path: Path) -> None:
    database, user_id, _media = seed(tmp_path)
    with database.session() as session:
        payload = export_user_data(session, user_id)

    assert payload["user"]["whatsapp_jid"] == OWNER
    assert len(payload["messages"]) == 3
    assert payload["memories"][0]["content"] == "Dr Sharma"
    assert payload["reminders"][0]["title"] == "Call Ramesh"
    assert payload["documents"][0]["filename"] == "bill.pdf"
    # JSON-serializable end to end, which is the point of an export.
    json.dumps(payload)


def test_written_export_is_owner_only(tmp_path: Path) -> None:
    database, user_id, _media = seed(tmp_path)
    target = tmp_path / "exports" / "dump.json"
    with database.session() as session:
        path = write_export(session, user_id, target)

    assert path.exists()
    assert path.stat().st_mode & 0o077 == 0  # no group or other access
    assert json.loads(path.read_text())["memories"][0]["content"] == "Dr Sharma"


def test_purge_removes_everything_including_media(tmp_path: Path) -> None:
    database, user_id, media_dir = seed(tmp_path)
    with database.session() as session:
        counts = purge_user_data(session, user_id, media_dir)

    assert counts["messages"] == 3
    assert counts["memories"] == 1
    assert counts["media_files"] == 2
    with database.session() as session:
        for model in (Message, Memory, Reminder, Document, User):
            assert session.scalar(select(model)) is None
    assert list(media_dir.iterdir()) == []


def test_retention_expires_raw_material_but_keeps_what_is_cited(tmp_path: Path) -> None:
    database, user_id, media_dir = seed(tmp_path)
    with database.session() as session:
        counts = apply_retention(session, user_id, retention_days=365, media_dir=media_dir)

    assert counts["documents"] == 1
    with database.session() as session:
        remaining = {m.whatsapp_message_id for m in session.scalars(select(Message)).all()}
        # "chatter" was old and cited by nothing, so it went.
        assert "chatter" not in remaining
        # "cited" is old too, but a memory points at it, so provenance survives.
        assert "cited" in remaining
        assert "recent" in remaining
        # The curated output is untouched.
        assert session.scalar(select(Memory)).content == "Dr Sharma"
        assert session.scalar(select(Reminder)).title == "Call Ramesh"

    assert not (media_dir / "old.ogg").exists()
    assert not (media_dir / "bill.pdf").exists()


def test_retention_disabled_changes_nothing(tmp_path: Path) -> None:
    database, user_id, media_dir = seed(tmp_path)
    with database.session() as session:
        counts = apply_retention(session, user_id, retention_days=0, media_dir=media_dir)

    assert counts == {"messages": 0, "documents": 0, "media_files": 0, "inbound_jobs": 0, "deliveries": 0}
    with database.session() as session:
        assert len(session.scalars(select(Message)).all()) == 3


def test_retention_never_deletes_outside_the_media_directory(tmp_path: Path) -> None:
    """A stored path is data; it must not be able to point the cleaner at /etc."""
    database, user_id, media_dir = seed(tmp_path)
    outsider = tmp_path / "not-media.txt"
    outsider.write_text("keep me")
    with database.session() as session:
        document = session.scalar(select(Document))
        document.storage_path = str(outsider)
        document.created_at = datetime.now(timezone.utc) - timedelta(days=500)

    with database.session() as session:
        apply_retention(session, user_id, retention_days=365, media_dir=media_dir)

    assert outsider.exists()
