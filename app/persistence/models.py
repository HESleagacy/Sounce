from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_jid: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(100))
    chat_jid: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    direction: Mapped[str] = mapped_column(String(20))
    message_type: Mapped[str] = mapped_column(String(30))
    text: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    media_path: Mapped[str | None] = mapped_column(Text)
    detected_languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(30), default="active")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recurrence_frequency: Mapped[str | None] = mapped_column(String(20))
    recurrence_interval: Mapped[int] = mapped_column(default=1)
    category: Mapped[str | None] = mapped_column(String(100))
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    calendar_sync_status: Mapped[str] = mapped_column(String(20), default="not_required")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    category: Mapped[str | None] = mapped_column(String(100))
    calendar_event_id: Mapped[str | None] = mapped_column(String(255))
    calendar_sync_status: Mapped[str] = mapped_column(String(20), default="not_required")
    status: Mapped[str] = mapped_column(String(30), default="active")


class Preference(Base):
    __tablename__ = "preferences"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_preferences_user_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str | None] = mapped_column(String(100))
    extracted_dates: Mapped[list[str]] = mapped_column(JSON, default=list)
    extracted_amounts: Mapped[list[str]] = mapped_column(JSON, default=list)
    extracted_entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InboundJob(Base):
    """Durable inbox.

    Every accepted WhatsApp message is committed here *before* it reaches the
    in-memory worker queue, so a full queue, a crash, or a restart cannot lose
    an input. The in-memory queue is only a latency optimisation; this table is
    the source of truth for what still has to be processed.
    """

    __tablename__ = "inbound_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ReminderDelivery(Base):
    """One row per reminder *occurrence*, which is what makes delivery idempotent.

    ``occurrence_key`` is the UTC due time the delivery is for, so a recurring
    reminder gets a distinct row per firing. The unique constraint is the guard:
    once a row reaches ``sent`` the same occurrence can never be delivered again.
    """

    __tablename__ = "reminder_deliveries"
    __table_args__ = (
        UniqueConstraint("reminder_id", "occurrence_key", name="uq_reminder_deliveries_occurrence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey("reminders.id"), index=True)
    occurrence_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="sending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarOp(Base):
    """Transactional outbox for Google Calendar.

    The decision engine never calls Calendar inline. It commits an op in the same
    transaction that changes local state, so the two can't diverge: either both
    land or neither does. A worker drains ops with retries and writes the remote
    event id back, so a successful remote create is never lost.
    """

    __tablename__ = "calendar_ops"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    op: Mapped[str] = mapped_column(String(20))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


Index("ix_inbound_jobs_status_available", InboundJob.status, InboundJob.available_at)
Index("ix_calendar_ops_status_available", CalendarOp.status, CalendarOp.available_at)
