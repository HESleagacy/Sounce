from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.messages import (
    InboundMessage,
    MessageDirection,
    OutboundMessage,
    inbound_to_payload,
)
from app.domain.reminders import as_utc
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


class Repository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create_user(self, jid: str, timezone_name: str) -> User:
        user = self.session.scalar(select(User).where(User.whatsapp_jid == jid))
        if user is None:
            user = User(whatsapp_jid=jid, timezone=timezone_name)
            self.session.add(user)
            self.session.flush()
        return user

    def has_message(self, whatsapp_message_id: str) -> bool:
        return (
            self.session.scalar(select(Message.id).where(Message.whatsapp_message_id == whatsapp_message_id))
            is not None
        )

    def is_known_outbound(self, whatsapp_message_id: str) -> bool:
        return (
            self.session.scalar(
                select(Message.id).where(
                    Message.whatsapp_message_id == whatsapp_message_id,
                    Message.direction == MessageDirection.OUTBOUND.value,
                )
            )
            is not None
        )

    def add_inbound(self, user: User, inbound: InboundMessage) -> Message | None:
        message = Message(
            whatsapp_message_id=inbound.whatsapp_message_id,
            user_id=user.id,
            direction=MessageDirection.INBOUND.value,
            message_type=inbound.message_type.value,
            text=inbound.text,
            created_at=inbound.occurred_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(message)
                self.session.flush()
        except IntegrityError:
            return None
        return message

    def add_outbound(self, user: User, outbound: OutboundMessage) -> Message | None:
        message = Message(
            whatsapp_message_id=outbound.whatsapp_message_id,
            user_id=user.id,
            direction=MessageDirection.OUTBOUND.value,
            message_type="text",
            text=outbound.text,
            created_at=outbound.occurred_at,
        )
        try:
            with self.session.begin_nested():
                self.session.add(message)
                self.session.flush()
        except IntegrityError:
            return None
        return message

    def list_recent_messages(self, user_id: int, limit: int = 8) -> list[Message]:
        rows = list(
            self.session.scalars(
                select(Message).where(Message.user_id == user_id).order_by(Message.id.desc()).limit(limit)
            )
        )
        return list(reversed(rows))

    def list_memories(self, user_id: int, limit: int = 30) -> list[Memory]:
        return list(
            self.session.scalars(
                select(Memory)
                .where(Memory.user_id == user_id, Memory.status == "active")
                .order_by(Memory.updated_at.desc())
                .limit(limit)
            )
        )

    def add_memory(
        self,
        user_id: int,
        source_message_id: int,
        kind: str,
        category: str,
        content: str,
        confidence: float,
    ) -> Memory:
        memory = Memory(
            user_id=user_id,
            source_message_id=source_message_id,
            kind=kind,
            category=category,
            content=content,
            confidence=confidence,
        )
        self.session.add(memory)
        self.session.flush()
        return memory

    def get_preferences(self, user_id: int) -> dict[str, str]:
        preferences = self.session.scalars(select(Preference).where(Preference.user_id == user_id))
        return {preference.key: preference.value for preference in preferences}

    def set_preference(self, user_id: int, source_message_id: int, key: str, value: str) -> Preference:
        preference = self.session.scalar(
            select(Preference).where(Preference.user_id == user_id, Preference.key == key)
        )
        if preference is None:
            preference = Preference(
                user_id=user_id,
                key=key,
                value=value,
                source_message_id=source_message_id,
            )
            self.session.add(preference)
        else:
            preference.value = value
            preference.source_message_id = source_message_id
            preference.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return preference

    def get_pending_action(self, user_id: int) -> PendingAction | None:
        now = datetime.now(timezone.utc)
        pending = self.session.scalar(
            select(PendingAction)
            .where(PendingAction.user_id == user_id, PendingAction.status == "pending")
            .order_by(PendingAction.id.desc())
        )
        if pending is not None:
            expires_at = pending.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                pending.status = "expired"
                return None
        return pending

    def replace_pending_action(
        self,
        user_id: int,
        source_message_id: int,
        action_type: str,
        payload: dict[str, object],
        ttl_minutes: int,
    ) -> PendingAction:
        for existing in self.session.scalars(
            select(PendingAction).where(PendingAction.user_id == user_id, PendingAction.status == "pending")
        ):
            existing.status = "superseded"
        pending = PendingAction(
            user_id=user_id,
            source_message_id=source_message_id,
            action_type=action_type,
            payload=payload,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        )
        self.session.add(pending)
        self.session.flush()
        return pending

    def resolve_pending_action(self, pending: PendingAction | None) -> None:
        if pending is not None:
            pending.status = "resolved"

    def update_chat_jid(self, user: User, chat_jid: str) -> None:
        if user.chat_jid != chat_jid:
            user.chat_jid = chat_jid
            self.session.flush()

    def get_memory(self, user_id: int, memory_id: int) -> Memory | None:
        return self.session.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))

    def forget_memory(self, user_id: int, memory_id: int) -> bool:
        memory = self.get_memory(user_id, memory_id)
        if memory is None or memory.status != "active":
            return False
        memory.status = "forgotten"
        memory.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return True

    def add_reminder(
        self,
        user_id: int,
        source_message_id: int,
        title: str,
        due_at: datetime,
        timezone_name: str,
        recurrence_frequency: str | None = None,
        recurrence_interval: int = 1,
        category: str | None = None,
    ) -> Reminder:
        reminder = Reminder(
            user_id=user_id,
            source_message_id=source_message_id,
            title=title,
            due_at=due_at,
            timezone=timezone_name,
            recurrence_frequency=recurrence_frequency,
            recurrence_interval=recurrence_interval,
            category=category,
        )
        self.session.add(reminder)
        self.session.flush()
        return reminder

    def get_reminder(self, user_id: int, reminder_id: int) -> Reminder | None:
        return self.session.scalar(
            select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == user_id)
        )

    def cancel_reminder(self, user_id: int, reminder_id: int) -> bool:
        reminder = self.get_reminder(user_id, reminder_id)
        if reminder is None or reminder.status != "pending":
            return False
        reminder.status = "cancelled"
        self.session.flush()
        return True

    def reschedule_reminder(self, user_id: int, reminder_id: int, due_at: datetime) -> bool:
        reminder = self.get_reminder(user_id, reminder_id)
        if reminder is None or reminder.status != "pending":
            return False
        reminder.due_at = due_at
        reminder.delivered_at = None
        self.session.flush()
        return True

    def list_upcoming_reminders(self, user_id: int, limit: int = 10) -> list[Reminder]:
        return list(
            self.session.scalars(
                select(Reminder)
                .where(Reminder.user_id == user_id, Reminder.status == "pending")
                .order_by(Reminder.due_at.asc())
                .limit(limit)
            )
        )

    def add_timeline_event(
        self,
        user_id: int,
        source_message_id: int,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        category: str | None = None,
    ) -> TimelineEvent:
        event = TimelineEvent(
            user_id=user_id,
            source_message_id=source_message_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            category=category,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def list_upcoming_timeline_events(self, user_id: int, limit: int = 10) -> list[TimelineEvent]:
        now = datetime.now(timezone.utc)
        events = self.session.scalars(
            select(TimelineEvent)
            .where(TimelineEvent.user_id == user_id, TimelineEvent.status == "active")
            .order_by(TimelineEvent.starts_at.asc())
        )
        upcoming = [event for event in events if as_utc(event.ends_at) > now]
        return upcoming[:limit]

    def add_document(
        self,
        user_id: int,
        source_message_id: int,
        filename: str,
        mime_type: str,
        storage_path: str,
        summary: str | None,
        extracted_text: str | None,
        document_type: str | None = None,
        extracted_dates: list[str] | None = None,
        extracted_amounts: list[str] | None = None,
        extracted_entities: list[str] | None = None,
    ) -> Document:
        document = Document(
            user_id=user_id,
            source_message_id=source_message_id,
            filename=filename,
            mime_type=mime_type,
            storage_path=storage_path,
            summary=summary,
            extracted_text=extracted_text,
            document_type=document_type,
            extracted_dates=extracted_dates or [],
            extracted_amounts=extracted_amounts or [],
            extracted_entities=extracted_entities or [],
        )
        self.session.add(document)
        self.session.flush()
        return document

    def list_recent_documents(self, user_id: int, limit: int = 5) -> list[Document]:
        return list(
            self.session.scalars(
                select(Document).where(Document.user_id == user_id).order_by(Document.id.desc()).limit(limit)
            )
        )

    # ------------------------------------------------------------------
    # Durable inbox
    # ------------------------------------------------------------------

    def enqueue_inbound_job(self, inbound: InboundMessage) -> InboundJob | None:
        """Commit an inbound message to the inbox. Returns None if already known."""
        job = InboundJob(
            whatsapp_message_id=inbound.whatsapp_message_id,
            payload=inbound_to_payload(inbound),
            status="queued",
            available_at=datetime.now(timezone.utc),
        )
        try:
            with self.session.begin_nested():
                self.session.add(job)
                self.session.flush()
        except IntegrityError:
            return None
        return job

    def claim_inbound_job(self, job_id: int, lease_seconds: int) -> InboundJob | None:
        """Take a lease on one queued job. Returns None if another worker won it."""
        now = datetime.now(timezone.utc)
        claimed = self.session.execute(
            update(InboundJob)
            .where(
                InboundJob.id == job_id,
                InboundJob.status == "queued",
                InboundJob.available_at <= now,
            )
            .values(
                status="processing",
                leased_until=now + timedelta(seconds=lease_seconds),
                attempts=InboundJob.attempts + 1,
            )
        )
        if rowcount(claimed) != 1:
            return None
        return self.session.get(InboundJob, job_id)

    def due_inbound_job_ids(self, limit: int = 50) -> list[int]:
        now = datetime.now(timezone.utc)
        return list(
            self.session.scalars(
                select(InboundJob.id)
                .where(InboundJob.status == "queued", InboundJob.available_at <= now)
                .order_by(InboundJob.id.asc())
                .limit(limit)
            )
        )

    def reclaim_expired_inbound_jobs(self) -> int:
        """Return jobs whose worker died mid-flight to the queue."""
        now = datetime.now(timezone.utc)
        released = self.session.execute(
            update(InboundJob)
            .where(InboundJob.status == "processing", InboundJob.leased_until < now)
            .values(status="queued", leased_until=None)
        )
        return rowcount(released)

    def finish_inbound_job(self, job_id: int, status: str = "done") -> None:
        job = self.session.get(InboundJob, job_id)
        if job is None:
            return
        job.status = status
        job.leased_until = None
        job.updated_at = datetime.now(timezone.utc)
        self.session.flush()

    def fail_inbound_job(
        self,
        job_id: int,
        error: str,
        max_attempts: int,
        backoff_seconds: int,
    ) -> str:
        """Reschedule a failed job, or dead-letter it once attempts run out."""
        job = self.session.get(InboundJob, job_id)
        if job is None:
            return "missing"
        job.last_error = error[:2000]
        job.leased_until = None
        job.updated_at = datetime.now(timezone.utc)
        if job.attempts >= max_attempts:
            job.status = "dead"
            self.session.flush()
            return "dead"
        job.status = "queued"
        job.available_at = datetime.now(timezone.utc) + timedelta(
            seconds=backoff_seconds * (2 ** (job.attempts - 1))
        )
        self.session.flush()
        return "queued"

    def count_inbound_jobs(self, status: str) -> int:
        return int(
            self.session.scalar(select(func.count(InboundJob.id)).where(InboundJob.status == status)) or 0
        )

    # ------------------------------------------------------------------
    # Reminder delivery ledger
    # ------------------------------------------------------------------

    def begin_delivery(self, reminder_id: int, occurrence_key: str, max_attempts: int) -> str:
        """Open a delivery attempt for one reminder occurrence.

        Returns "proceed" to send, "already_sent" when this exact occurrence has
        been committed as delivered (the idempotency guard), "dead" when the
        attempt budget is exhausted, or "raced" when another worker got there.
        """
        existing = self.session.scalar(
            select(ReminderDelivery).where(
                ReminderDelivery.reminder_id == reminder_id,
                ReminderDelivery.occurrence_key == occurrence_key,
            )
        )
        if existing is None:
            delivery = ReminderDelivery(
                reminder_id=reminder_id,
                occurrence_key=occurrence_key,
                status="sending",
                attempts=1,
            )
            try:
                with self.session.begin_nested():
                    self.session.add(delivery)
                    self.session.flush()
            except IntegrityError:
                return "raced"
            return "proceed"
        if existing.status == "sent":
            return "already_sent"
        if existing.attempts >= max_attempts:
            existing.status = "dead"
            self.session.flush()
            return "dead"
        existing.attempts += 1
        existing.status = "sending"
        self.session.flush()
        return "proceed"

    def complete_delivery(self, reminder_id: int, occurrence_key: str) -> None:
        delivery = self.session.scalar(
            select(ReminderDelivery).where(
                ReminderDelivery.reminder_id == reminder_id,
                ReminderDelivery.occurrence_key == occurrence_key,
            )
        )
        if delivery is None:
            return
        delivery.status = "sent"
        delivery.completed_at = datetime.now(timezone.utc)
        self.session.flush()

    def fail_delivery(self, reminder_id: int, occurrence_key: str, error: str) -> None:
        delivery = self.session.scalar(
            select(ReminderDelivery).where(
                ReminderDelivery.reminder_id == reminder_id,
                ReminderDelivery.occurrence_key == occurrence_key,
            )
        )
        if delivery is None:
            return
        delivery.status = "failed"
        delivery.last_error = error[:2000]
        self.session.flush()

    # ------------------------------------------------------------------
    # Calendar outbox
    # ------------------------------------------------------------------

    def enqueue_calendar_op(
        self,
        user_id: int,
        op: str,
        entity_type: str,
        entity_id: int,
        payload: dict[str, object],
    ) -> CalendarOp:
        record = CalendarOp(
            user_id=user_id,
            operation_id=uuid.uuid4().hex,
            op=op,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            status="pending",
            available_at=datetime.now(timezone.utc),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def due_calendar_op_ids(self, limit: int = 20) -> list[int]:
        now = datetime.now(timezone.utc)
        return list(
            self.session.scalars(
                select(CalendarOp.id)
                .where(CalendarOp.status == "pending", CalendarOp.available_at <= now)
                .order_by(CalendarOp.id.asc())
                .limit(limit)
            )
        )

    def mark_calendar_op_synced(self, op_id: int, event_id: str | None) -> None:
        record = self.session.get(CalendarOp, op_id)
        if record is None:
            return
        record.status = "synced"
        record.updated_at = datetime.now(timezone.utc)
        entity = self.calendar_entity(record)
        if entity is not None:
            if event_id:
                entity.calendar_event_id = event_id
            entity.calendar_sync_status = "synced" if record.op != "delete" else "deleted"
        self.session.flush()

    def mark_calendar_op_failed(
        self,
        op_id: int,
        error: str,
        max_attempts: int,
        backoff_seconds: int,
    ) -> str:
        record = self.session.get(CalendarOp, op_id)
        if record is None:
            return "missing"
        record.attempts += 1
        record.last_error = error[:2000]
        record.updated_at = datetime.now(timezone.utc)
        entity = self.calendar_entity(record)
        if record.attempts >= max_attempts:
            record.status = "failed"
            if entity is not None:
                entity.calendar_sync_status = "failed"
            self.session.flush()
            return "failed"
        record.available_at = datetime.now(timezone.utc) + timedelta(
            seconds=backoff_seconds * (2 ** (record.attempts - 1))
        )
        self.session.flush()
        return "pending"

    def calendar_entity(self, record: CalendarOp) -> Reminder | TimelineEvent | None:
        if record.entity_type == "reminder":
            return self.session.get(Reminder, record.entity_id)
        if record.entity_type == "timeline_event":
            return self.session.get(TimelineEvent, record.entity_id)
        return None

    def count_calendar_ops(self, status: str) -> int:
        return int(
            self.session.scalar(select(func.count(CalendarOp.id)).where(CalendarOp.status == status)) or 0
        )
