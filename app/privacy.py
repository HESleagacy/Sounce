"""Data lifecycle: export, erase, and retention.

This assistant stores the most personal thing a person has on their phone — the
notes they write to themselves — plus transcripts of their voice and the
documents they forward. A store like that needs an exit. Every fact the app
holds must be exportable in a readable form and erasable on request, and the
raw material must not accumulate forever by default.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.persistence.database import rowcount
from app.persistence.models import (
    CalendarOp,
    Document,
    InboundJob,
    Memory,
    Message,
    PendingAction,
    Preference,
    Reminder,
    ReminderDelivery,
    TimelineEvent,
    User,
)

log = logging.getLogger(__name__)


def _rows(session: Session, model: Any, user_id: int) -> list[dict[str, Any]]:
    records = session.scalars(select(model).where(model.user_id == user_id))
    return [
        {column.name: _plain(getattr(record, column.name)) for column in model.__table__.columns}
        for record in records
    ]


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def export_user_data(session: Session, user_id: int) -> dict[str, Any]:
    """Everything the app knows about one user, as plain JSON-ready data."""
    user = session.get(User, user_id)
    reminder_ids = list(session.scalars(select(Reminder.id).where(Reminder.user_id == user_id)))
    deliveries: list[ReminderDelivery] = (
        list(session.scalars(select(ReminderDelivery).where(ReminderDelivery.reminder_id.in_(reminder_ids))))
        if reminder_ids
        else []
    )
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user": {column.name: _plain(getattr(user, column.name)) for column in User.__table__.columns}
        if user is not None
        else None,
        "messages": _rows(session, Message, user_id),
        "memories": _rows(session, Memory, user_id),
        "reminders": _rows(session, Reminder, user_id),
        "reminder_deliveries": [
            {
                column.name: _plain(getattr(record, column.name))
                for column in ReminderDelivery.__table__.columns
            }
            for record in deliveries
        ],
        "timeline_events": _rows(session, TimelineEvent, user_id),
        "preferences": _rows(session, Preference, user_id),
        "pending_actions": _rows(session, PendingAction, user_id),
        "documents": _rows(session, Document, user_id),
        "calendar_ops": _rows(session, CalendarOp, user_id),
    }


def write_export(session: Session, user_id: int, destination: Path) -> Path:
    payload = export_user_data(session, user_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    # The export contains every message and transcript; keep it owner-only.
    destination.chmod(0o600)
    return destination


def purge_user_data(session: Session, user_id: int, media_dir: Path | None = None) -> dict[str, int]:
    """Erase everything for a user, including media on disk.

    Deletion order follows the foreign keys: anything pointing at a message goes
    before the messages themselves.
    """
    removed_media = _delete_media_for(session, user_id, media_dir)

    reminder_ids = list(session.scalars(select(Reminder.id).where(Reminder.user_id == user_id)))
    counts: dict[str, int] = {}
    if reminder_ids:
        counts["reminder_deliveries"] = _delete(
            session, delete(ReminderDelivery).where(ReminderDelivery.reminder_id.in_(reminder_ids))
        )
    for label, statement in (
        ("calendar_ops", delete(CalendarOp).where(CalendarOp.user_id == user_id)),
        ("pending_actions", delete(PendingAction).where(PendingAction.user_id == user_id)),
        ("memories", delete(Memory).where(Memory.user_id == user_id)),
        ("documents", delete(Document).where(Document.user_id == user_id)),
        ("timeline_events", delete(TimelineEvent).where(TimelineEvent.user_id == user_id)),
        ("reminders", delete(Reminder).where(Reminder.user_id == user_id)),
        ("preferences", delete(Preference).where(Preference.user_id == user_id)),
        ("messages", delete(Message).where(Message.user_id == user_id)),
        ("inbound_jobs", delete(InboundJob)),
        ("users", delete(User).where(User.id == user_id)),
    ):
        counts[label] = _delete(session, statement)
    counts["media_files"] = removed_media
    session.flush()
    log.warning("Purged all stored data for user %s: %s", user_id, counts)
    return counts


def apply_retention(
    session: Session,
    user_id: int,
    retention_days: int,
    media_dir: Path | None = None,
) -> dict[str, int]:
    """Drop raw material older than the retention window.

    Curated state — memories, reminders, preferences, timeline events — is kept:
    it is the product. What expires is the raw feed behind it, and only where it
    is not cited as the source of something still in use, so "how do you know
    that?" keeps working for anything the assistant still claims to remember.
    """
    if retention_days <= 0:
        return {"messages": 0, "documents": 0, "media_files": 0, "inbound_jobs": 0, "deliveries": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cited = _cited_message_ids(session, user_id)

    expired_documents = list(
        session.scalars(select(Document).where(Document.user_id == user_id, Document.created_at < cutoff))
    )
    document_count = 0
    media_removed = 0
    for document in expired_documents:
        media_removed += _unlink(document.storage_path, media_dir)
        session.delete(document)
        document_count += 1
    session.flush()

    cited = _cited_message_ids(session, user_id)
    expired_messages = [
        message
        for message in session.scalars(
            select(Message).where(Message.user_id == user_id, Message.created_at < cutoff)
        )
        if message.id not in cited
    ]
    message_count = 0
    for message in expired_messages:
        media_removed += _unlink(message.media_path, media_dir)
        session.delete(message)
        message_count += 1

    jobs = _delete(
        session,
        delete(InboundJob).where(InboundJob.status.in_(("done", "dead")), InboundJob.created_at < cutoff),
    )
    deliveries = _delete(
        session,
        delete(ReminderDelivery).where(
            ReminderDelivery.status == "sent", ReminderDelivery.created_at < cutoff
        ),
    )
    session.flush()

    counts = {
        "messages": message_count,
        "documents": document_count,
        "media_files": media_removed,
        "inbound_jobs": jobs,
        "deliveries": deliveries,
    }
    if any(counts.values()):
        log.info("Retention pass removed %s", counts)
    return counts


def _cited_message_ids(session: Session, user_id: int) -> set[int]:
    """Message ids still referenced as provenance by something the user relies on."""
    cited: set[int] = set()
    for model in (Memory, Reminder, TimelineEvent, Preference, PendingAction, Document):
        cited.update(
            value
            for value in session.scalars(select(model.source_message_id).where(model.user_id == user_id))
            if value is not None
        )
    return cited


def _delete_media_for(session: Session, user_id: int, media_dir: Path | None) -> int:
    removed = 0
    for path in session.scalars(
        select(Message.media_path).where(Message.user_id == user_id, Message.media_path.is_not(None))
    ):
        removed += _unlink(path, media_dir)
    for path in session.scalars(select(Document.storage_path).where(Document.user_id == user_id)):
        removed += _unlink(path, media_dir)
    return removed


def _unlink(stored_path: str | None, media_dir: Path | None) -> int:
    """Delete one stored media file, refusing anything outside the media directory."""
    if not stored_path:
        return 0
    path = Path(stored_path)
    if not path.is_absolute() and media_dir is not None and media_dir not in path.parents:
        path = media_dir / path.name
    if media_dir is not None:
        try:
            path.resolve().relative_to(media_dir.resolve())
        except (ValueError, OSError):
            # A web-page "path" is a URL, not a file; nothing to delete.
            return 0
    try:
        path.unlink()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return 0
    return 1


def _delete(session: Session, statement: Any) -> int:
    return rowcount(session.execute(statement))
