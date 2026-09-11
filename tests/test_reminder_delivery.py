"""Reminder delivery semantics: the ledger, the lease, and what a crash costs."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.messages import InboundMessage, MessageType, OutboundMessage
from app.persistence.database import Database
from app.persistence.models import Base, Reminder, ReminderDelivery
from app.persistence.repositories import Repository
from app.workers.reminder_worker import ReminderWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


class RecordingTransport:
    def __init__(self, fail_times: int = 0, crash_after_send: bool = False) -> None:
        self.sent: list[str] = []
        self.fail_times = fail_times
        self.crash_after_send = crash_after_send

    def set_message_handler(self, _handler: object) -> None:
        pass

    def send_text(self, chat_jid: str, text: str) -> OutboundMessage:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("whatsapp down")
        self.sent.append(text)
        if self.crash_after_send:
            # Stands in for the process dying between the send and its commit.
            raise SystemExit("crash after send")
        return OutboundMessage(
            whatsapp_message_id=f"out-{len(self.sent)}",
            chat_jid=chat_jid,
            text=text,
            occurred_at=datetime.now(timezone.utc),
        )

    def send_voice_note(self, chat_jid: str, audio: bytes) -> OutboundMessage:
        raise AssertionError("unused")

    def download_media(self, whatsapp_message_id: str) -> bytes | None:
        return None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def seed(tmp_path: Path, **reminder_kwargs) -> tuple[Database, int]:
    database = Database(f"sqlite:///{tmp_path / 'reminders.db'}")
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        repository.update_chat_jid(user, OWNER)
        source = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id="seed",
                chat_jid=OWNER,
                sender_jid=OWNER,
                message_type=MessageType.TEXT,
                text="reminder",
                occurred_at=datetime.now(timezone.utc),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        reminder = repository.add_reminder(
            user_id=user.id,
            source_message_id=source.id,
            title="Call Ramesh",
            due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            timezone_name="Asia/Kolkata",
            **reminder_kwargs,
        )
        reminder_id = reminder.id
    return database, reminder_id


def test_a_due_reminder_is_delivered_exactly_once(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    transport = RecordingTransport()
    worker = ReminderWorker(database, transport, OWNER)

    assert worker.deliver_due_reminders() == 1
    # Polling again must not resend it.
    assert worker.deliver_due_reminders() == 0
    assert len(transport.sent) == 1

    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "delivered"
        delivery = session.scalar(select(ReminderDelivery))
        assert delivery.status == "sent"
        assert delivery.attempts == 1


def test_a_committed_occurrence_is_never_resent(tmp_path: Path) -> None:
    """The idempotency guard, tested directly.

    Even if the reminder is forced back to pending — which is exactly what the
    lease reclaimer does after a crash — an occurrence already recorded as sent
    must not go out a second time.
    """
    database, reminder_id = seed(tmp_path)
    transport = RecordingTransport()
    worker = ReminderWorker(database, transport, OWNER)
    worker.deliver_due_reminders()
    assert len(transport.sent) == 1

    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = "pending"
        reminder.lease_expires_at = None

    assert worker.deliver_due_reminders() == 0
    assert len(transport.sent) == 1


def test_a_crash_between_send_and_commit_costs_at_most_one_duplicate(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    crashing = RecordingTransport(crash_after_send=True)
    worker = ReminderWorker(database, crashing, OWNER, lease_seconds=-1)

    with suppress(SystemExit):
        worker.deliver_due_reminders()
    assert len(crashing.sent) == 1
    with database.session() as session:
        # The claim and the delivery row are durable even though nothing committed after.
        assert session.get(Reminder, reminder_id).status == "delivering"
        assert session.scalar(select(ReminderDelivery)).status == "sending"

    # Restart with a working transport: the expired lease is reclaimed and retried.
    healthy = RecordingTransport()
    restarted = ReminderWorker(database, healthy, OWNER)
    assert restarted.deliver_due_reminders() == 1
    assert len(healthy.sent) == 1  # one duplicate overall, then it settles

    # And it stops there.
    assert restarted.deliver_due_reminders() == 0
    assert len(healthy.sent) == 1


def test_transport_failure_returns_the_reminder_for_retry(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    transport = RecordingTransport(fail_times=1)
    worker = ReminderWorker(database, transport, OWNER)

    assert worker.deliver_due_reminders() == 0
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "pending"
        assert reminder.delivery_attempts == 1
        assert "whatsapp down" in reminder.last_error

    assert worker.deliver_due_reminders() == 1
    assert len(transport.sent) == 1


def test_delivery_gives_up_after_the_attempt_budget(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    transport = RecordingTransport(fail_times=99)
    worker = ReminderWorker(database, transport, OWNER, max_attempts=2)

    for _ in range(4):
        worker.deliver_due_reminders()

    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "undeliverable"
    assert transport.sent == []


def test_a_recurring_reminder_advances_and_gets_its_own_ledger_row(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path, recurrence_frequency="daily", recurrence_interval=1)
    transport = RecordingTransport()
    worker = ReminderWorker(database, transport, OWNER)

    assert worker.deliver_due_reminders() == 1
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "pending"
        assert reminder.due_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        first_due = reminder.due_at

    # Force the next occurrence due; it is a different occurrence_key, so it sends.
    with database.session() as session:
        session.get(Reminder, reminder_id).due_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert worker.deliver_due_reminders() == 1
    assert len(transport.sent) == 2

    with database.session() as session:
        keys = {row.occurrence_key for row in session.scalars(select(ReminderDelivery)).all()}
        assert len(keys) == 2
        assert first_due is not None


def test_quiet_hours_defer_without_burning_an_attempt(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    with database.session() as session:
        repository = Repository(session)
        user_id = session.scalar(select(Reminder.user_id).where(Reminder.id == reminder_id))
        source_id = session.scalar(select(Reminder.source_message_id).where(Reminder.id == reminder_id))
        # A window that covers the whole day, so "now" is always quiet.
        repository.set_preference(user_id, source_id, "quiet_hours_start", "00:00")
        repository.set_preference(user_id, source_id, "quiet_hours_end", "23:59")

    transport = RecordingTransport()
    worker = ReminderWorker(database, transport, OWNER)
    assert worker.deliver_due_reminders() == 0

    assert transport.sent == []
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "pending"
        assert reminder.delivery_attempts == 0
        assert session.scalar(select(ReminderDelivery)) is None


def test_an_expired_lease_is_reclaimed(tmp_path: Path) -> None:
    database, reminder_id = seed(tmp_path)
    transport = RecordingTransport()
    worker = ReminderWorker(database, transport, OWNER)
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = "delivering"
        reminder.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    assert worker.reclaim_expired_leases() == 1
    with database.session() as session:
        assert session.get(Reminder, reminder_id).status == "pending"
