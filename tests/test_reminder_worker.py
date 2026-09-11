from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.messages import InboundMessage, MessageType, OutboundMessage
from app.domain.preferences import next_allowed_time
from app.domain.reminders import as_utc, parse_natural_interval, parse_natural_schedule
from app.domain.timeline import Interval, find_conflicts, overlaps
from app.persistence.database import Database
from app.persistence.models import Base, Reminder
from app.persistence.repositories import Repository
from app.workers.reminder_worker import ReminderWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def set_message_handler(self, _handler: object) -> None:
        pass

    def send_text(self, chat_jid: str, text: str) -> OutboundMessage:
        outbound = OutboundMessage(
            whatsapp_message_id=f"out-{len(self.sent) + 1}",
            chat_jid=chat_jid,
            text=text,
            occurred_at=datetime.now(timezone.utc),
        )
        self.sent.append(outbound)
        return outbound

    def send_voice_note(self, chat_jid: str, audio: bytes) -> OutboundMessage:
        raise AssertionError("unused")

    def download_media(self, whatsapp_message_id: str) -> bytes | None:
        return None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def seed_reminder(
    database: Database,
    due_at: datetime,
    preferences: dict[str, str] | None = None,
    category: str | None = None,
) -> int:
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        repository.update_chat_jid(user, "280409992609926@lid")
        message = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id=f"seed-{due_at.timestamp()}",
                chat_jid="280409992609926@lid",
                sender_jid=OWNER,
                message_type=MessageType.TEXT,
                text="seed",
                occurred_at=datetime.now(timezone.utc),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        for key, value in (preferences or {}).items():
            repository.set_preference(user.id, message.id, key, value)
        reminder = repository.add_reminder(
            user_id=user.id,
            source_message_id=message.id,
            title="Call Ramesh",
            due_at=due_at,
            timezone_name="Asia/Kolkata",
            category=category,
        )
        return reminder.id


def make_worker(tmp_path: Path) -> tuple[ReminderWorker, Database, FakeTransport]:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(database.engine)
    transport = FakeTransport()
    worker = ReminderWorker(database, transport, OWNER, poll_seconds=1)
    return worker, database, transport


def test_due_reminder_is_delivered_to_the_self_chat(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    reminder_id = seed_reminder(database, datetime.now(timezone.utc) - timedelta(minutes=1))

    assert worker.deliver_due_reminders() == 1

    assert transport.sent[0].chat_jid == "280409992609926@lid"
    assert "Call Ramesh" in transport.sent[0].text
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "delivered"
        assert reminder.delivered_at is not None
        assert session.scalar(select(Reminder).where(Reminder.id == reminder_id)).status == "delivered"


def test_future_reminder_is_not_delivered(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    seed_reminder(database, datetime.now(timezone.utc) + timedelta(hours=1))

    assert worker.deliver_due_reminders() == 0
    assert transport.sent == []


def test_quiet_hours_defer_delivery(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    reminder_id = seed_reminder(
        database,
        datetime.now(timezone.utc) - timedelta(minutes=1),
        preferences={"quiet_hours_start": "00:00", "quiet_hours_end": "23:59"},
    )

    assert worker.deliver_due_reminders() == 0

    assert transport.sent == []
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "pending"
        assert as_utc(reminder.due_at) > datetime.now(timezone.utc)


def test_delivery_failure_keeps_reminder_pending(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    reminder_id = seed_reminder(database, datetime.now(timezone.utc) - timedelta(minutes=1))

    def failing_send(chat_jid: str, text: str) -> OutboundMessage:
        raise RuntimeError("network down")

    transport.send_text = failing_send  # type: ignore[method-assign]
    assert worker.deliver_due_reminders() == 0
    with database.session() as session:
        assert session.get(Reminder, reminder_id).status == "pending"


def test_next_allowed_time_rules() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    late_night = datetime(2026, 8, 8, 23, 30, tzinfo=tz)
    morning = datetime(2026, 8, 8, 9, 0, tzinfo=tz)

    deferred = next_allowed_time(late_night, "22:00", "07:00")
    assert deferred is not None and deferred.hour == 7 and deferred.day == 9

    assert next_allowed_time(morning, "22:00", "07:00") is None
    assert next_allowed_time(late_night, None, None) is None
    assert next_allowed_time(late_night, "10:00", "10:00") is None


def test_overlap_rule_matches_project_contract() -> None:
    base = datetime(2026, 8, 9, 16, 30, tzinfo=timezone.utc)
    event = Interval(title="Doctor", starts_at=base, ends_at=base + timedelta(hours=1))

    assert overlaps(
        base + timedelta(minutes=30), base + timedelta(minutes=90), event.starts_at, event.ends_at
    )
    assert not overlaps(base + timedelta(hours=2), base + timedelta(hours=3), event.starts_at, event.ends_at)
    assert find_conflicts([event], base + timedelta(minutes=30)) == [event]
    assert find_conflicts([event], base + timedelta(hours=2)) == []


def test_recurring_reminder_schedules_its_next_occurrence(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    reminder_id = seed_reminder(database, datetime.now(timezone.utc) - timedelta(minutes=1))
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.recurrence_frequency = "weekly"
        reminder.recurrence_interval = 1

    assert worker.deliver_due_reminders() == 1
    assert len(transport.sent) == 1
    with database.session() as session:
        reminder = session.get(Reminder, reminder_id)
        assert reminder.status == "pending"
        assert as_utc(reminder.due_at) > datetime.now(timezone.utc) + timedelta(days=6)


def test_category_quiet_hours_override_general_policy(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    reminder_id = seed_reminder(
        database,
        datetime.now(timezone.utc) - timedelta(minutes=1),
        preferences={
            "quiet_hours_start": "10:00",
            "quiet_hours_end": "10:00",
            "work_quiet_hours_start": "00:00",
            "work_quiet_hours_end": "23:59",
        },
        category="work",
    )

    assert worker.deliver_due_reminders() == 0
    assert transport.sent == []
    with database.session() as session:
        assert session.get(Reminder, reminder_id).status == "pending"


def test_hinglish_weekday_time_parser() -> None:
    now = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
    parsed = parse_natural_schedule(
        "Sunday shaam 6 baje",
        "Asia/Kolkata",
        now=now,
    )
    local = parsed.astimezone(ZoneInfo("Asia/Kolkata"))
    assert local.weekday() == 6
    assert (local.hour, local.minute) == (18, 0)


def test_hinglish_timeline_interval_parser() -> None:
    now = datetime(2026, 8, 8, 9, 0, tzinfo=timezone.utc)
    parsed = parse_natural_interval(
        "Kal shaam 4:30 se 5:30 doctor appointment",
        "Asia/Kolkata",
        now=now,
    )
    assert parsed is not None
    starts_at, ends_at = parsed
    local_start = starts_at.astimezone(ZoneInfo("Asia/Kolkata"))
    local_end = ends_at.astimezone(ZoneInfo("Asia/Kolkata"))
    assert (local_start.hour, local_start.minute) == (16, 30)
    assert (local_end.hour, local_end.minute) == (17, 30)
