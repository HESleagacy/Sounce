from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.assistant.decision_engine import DecisionEngine
from app.assistant.schemas import AssistantDecision, ProposedAction
from app.assistant.service import AssistantService
from app.domain.messages import InboundMessage, MessageType, OutboundMessage
from app.persistence.database import Database
from app.persistence.models import Base, CalendarOp, PendingAction, Reminder, TimelineEvent
from app.persistence.repositories import Repository
from app.workers.calendar_worker import CalendarWorker
from app.workers.message_worker import MessageWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


class FakeProvider:
    def __init__(self, decisions: list[AssistantDecision]) -> None:
        self.decisions = decisions
        self.contexts: list[str] = []

    def interpret(self, _message: str, context: str) -> AssistantDecision:
        self.contexts.append(context)
        return self.decisions.pop(0)


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
        raise AssertionError("voice should not be used in these tests")

    def download_media(self, whatsapp_message_id: str) -> bytes | None:
        return None

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def inbound(message_id: str, text: str) -> InboundMessage:
    return InboundMessage(
        whatsapp_message_id=message_id,
        chat_jid=OWNER,
        sender_jid=OWNER,
        message_type=MessageType.TEXT,
        text=text,
        occurred_at=datetime.now(timezone.utc),
        is_from_me=True,
        is_self_chat=True,
    )


def make_worker(
    tmp_path: Path, provider: FakeProvider, calendar=None
) -> tuple[MessageWorker, Database, FakeTransport]:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(database.engine)
    transport = FakeTransport()
    assistant = AssistantService(provider, DecisionEngine(pending_action_ttl_minutes=30, calendar=calendar))
    worker = MessageWorker(
        database, assistant, transport, OWNER, "Asia/Kolkata", media_dir=tmp_path / "media"
    )
    return worker, database, transport


def reminder_decision(due_at: datetime, title: str = "Call Ramesh") -> AssistantDecision:
    return AssistantDecision(
        intent="create_reminder",
        response="Reminder laga doon?",
        proposed_actions=[
            ProposedAction(action_type="create_reminder", title=title, scheduled_at=due_at.isoformat())
        ],
    )


def confirmation_decision() -> AssistantDecision:
    return AssistantDecision(intent="confirm_action", response="Reminder laga diya.")


def test_reminder_requires_confirmation_then_executes(tmp_path: Path) -> None:
    due_at = datetime.now(timezone.utc) + timedelta(hours=4)
    provider = FakeProvider([reminder_decision(due_at), confirmation_decision()])
    worker, database, transport = make_worker(tmp_path, provider)

    worker.process(inbound("in-1", "Kal 5 baje Ramesh ko call karna"))
    with database.session() as session:
        assert session.scalar(select(Reminder)) is None
        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        assert pending is not None and pending.payload["stage"] == "confirm"

    worker.process(inbound("in-2", "Haan"))
    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder is not None
        assert reminder.title == "Call Ramesh"
        assert reminder.status == "pending"
        pending = session.scalar(select(PendingAction))
        assert pending.status == "resolved"
    assert transport.sent[-1].text.endswith("Reminder laga diya.")


def test_non_confirming_reply_dismisses_pending_confirmation(tmp_path: Path) -> None:
    due_at = datetime.now(timezone.utc) + timedelta(hours=4)
    provider = FakeProvider(
        [
            reminder_decision(due_at),
            AssistantDecision(intent="answer_question", response="Theek hai, cancel."),
        ]
    )
    worker, database, _transport = make_worker(tmp_path, provider)

    worker.process(inbound("in-1", "Kal 5 baje Ramesh ko call karna"))
    worker.process(inbound("in-2", "Nahin rehne do"))

    with database.session() as session:
        assert session.scalar(select(Reminder)) is None
        pending = session.scalar(select(PendingAction))
        assert pending.status == "resolved"


def test_past_reminder_time_is_rejected_deterministically(tmp_path: Path) -> None:
    due_at = datetime.now(timezone.utc) - timedelta(hours=1)
    provider = FakeProvider([reminder_decision(due_at)])
    worker, database, transport = make_worker(tmp_path, provider)

    worker.process(inbound("in-1", "Reminder for earlier today"))

    with database.session() as session:
        assert session.scalar(select(Reminder)) is None
        assert session.scalar(select(PendingAction)) is None
    assert "guzar chuka" in transport.sent[-1].text


def test_conflict_note_is_appended_from_timeline(tmp_path: Path) -> None:
    event_start = datetime.now(timezone.utc) + timedelta(days=1)
    provider = FakeProvider([reminder_decision(event_start + timedelta(minutes=30), title="Call Ramesh")])
    worker, database, transport = make_worker(tmp_path, provider)

    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        message = repository.add_inbound(user, inbound("seed", "seed"))
        repository.add_timeline_event(
            user_id=user.id,
            source_message_id=message.id,
            title="Doctor appointment",
            starts_at=event_start,
            ends_at=event_start + timedelta(hours=1),
        )

    worker.process(inbound("in-1", "Us waqt Ramesh ko call karna"))

    assert "Doctor appointment" in transport.sent[-1].text
    with database.session() as session:
        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        scheduled_at = pending.payload["proposed_actions"][0]["scheduled_at"]
        assert datetime.fromisoformat(scheduled_at) == event_start + timedelta(hours=1)


def test_cancel_reminder_confirmation_flow(tmp_path: Path) -> None:
    due_at = datetime.now(timezone.utc) + timedelta(hours=6)
    provider = FakeProvider(
        [
            reminder_decision(due_at),
            confirmation_decision(),
        ]
    )
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Reminder set karo"))
    worker.process(inbound("in-2", "Haan"))

    with database.session() as session:
        reminder_id = session.scalar(select(Reminder)).id

    provider.decisions = [
        AssistantDecision(
            intent="answer_question",
            response="Cancel kar doon?",
            proposed_actions=[ProposedAction(action_type="cancel_reminder", reminder_id=reminder_id)],
        ),
        AssistantDecision(intent="confirm_action", response="Cancel kar diya."),
    ]
    worker.process(inbound("in-3", "Woh reminder cancel karo"))
    worker.process(inbound("in-4", "Haan"))

    with database.session() as session:
        assert session.scalar(select(Reminder)).status == "cancelled"


def test_timeline_event_created_after_confirmation(tmp_path: Path) -> None:
    starts_at = datetime.now(timezone.utc) + timedelta(days=2)
    provider = FakeProvider(
        [
            AssistantDecision(
                intent="create_reminder",
                response="Event bana doon?",
                proposed_actions=[
                    ProposedAction(
                        action_type="create_timeline_event",
                        title="Team standup",
                        starts_at=starts_at.isoformat(),
                        ends_at=(starts_at + timedelta(hours=1)).isoformat(),
                    )
                ],
            ),
            confirmation_decision(),
        ]
    )
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Parso standup schedule karo"))
    worker.process(inbound("in-2", "Haan"))

    with database.session() as session:
        event = session.scalar(select(TimelineEvent))
        assert event is not None and event.title == "Team standup"


def test_reschedule_reminder_requires_confirmation(tmp_path: Path) -> None:
    original = datetime.now(timezone.utc) + timedelta(hours=2)
    replacement = original + timedelta(days=1)
    provider = FakeProvider([reminder_decision(original), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Reminder set karo"))
    worker.process(inbound("in-2", "Haan"))
    with database.session() as session:
        reminder_id = session.scalar(select(Reminder)).id

    provider.decisions = [
        AssistantDecision(
            intent="create_reminder",
            response="Kal reschedule kar doon?",
            proposed_actions=[
                ProposedAction(
                    action_type="reschedule_reminder",
                    reminder_id=reminder_id,
                    scheduled_at=replacement.isoformat(),
                )
            ],
        ),
        confirmation_decision(),
    ]
    worker.process(inbound("in-3", "Kal kar do"))
    worker.process(inbound("in-4", "Haan"))

    with database.session() as session:
        assert session.scalar(select(Reminder)).due_at.replace(tzinfo=timezone.utc) == replacement


def test_explicit_reschedule_is_normalized_when_model_proposes_a_new_reminder(tmp_path: Path) -> None:
    original = datetime.now(timezone.utc) + timedelta(hours=2)
    replacement = original + timedelta(hours=8)
    provider = FakeProvider([reminder_decision(original), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Weekly reminder set karo"))
    worker.process(inbound("in-2", "Haan"))

    provider.decisions = [
        AssistantDecision(
            intent="create_reminder",
            response="Shaam ko kar doon?",
            proposed_actions=[
                ProposedAction(
                    action_type="create_reminder",
                    title="Duplicate reminder",
                    scheduled_at=replacement.isoformat(),
                    recurrence_frequency="weekly",
                )
            ],
        ),
        confirmation_decision(),
    ]
    worker.process(inbound("in-3", "Is reminder ko shaam ke liye reschedule kar do"))
    worker.process(inbound("in-4", "Haan"))

    with database.session() as session:
        reminders = session.scalars(select(Reminder)).all()
        assert len(reminders) == 1
        assert reminders[0].due_at.replace(tzinfo=timezone.utc) == replacement


def test_reschedule_time_falls_back_to_deterministic_hinglish_parser(tmp_path: Path) -> None:
    original = datetime.now(timezone.utc) + timedelta(hours=2)
    provider = FakeProvider([reminder_decision(original), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Reminder set karo"))
    worker.process(inbound("in-2", "Haan"))

    provider.decisions = [
        AssistantDecision(
            intent="clarify",
            response="Sunday shaam 6 baje kar doon?",
            proposed_actions=[
                ProposedAction(
                    action_type="reschedule_reminder",
                    reminder_id=1,
                    scheduled_at=None,
                )
            ],
        )
    ]
    worker.process(inbound("in-3", "Is reminder ko Sunday shaam 6 baje reschedule kar do"))

    with database.session() as session:
        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        action = pending.payload["proposed_actions"][0]
        parsed = datetime.fromisoformat(action["scheduled_at"])
        local = parsed.astimezone(timezone(timedelta(hours=5, minutes=30)))
        assert local.weekday() == 6
        assert (local.hour, local.minute) == (18, 0)
        assert pending.payload["stage"] == "confirm"


def test_explicit_reschedule_works_when_model_returns_no_action(tmp_path: Path) -> None:
    original = datetime.now(timezone.utc) + timedelta(hours=2)
    provider = FakeProvider([reminder_decision(original), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider)
    worker.process(inbound("in-1", "Reminder set karo"))
    worker.process(inbound("in-2", "Haan"))

    provider.decisions = [
        AssistantDecision(
            intent="answer_question",
            response="Yeh pehle hi reschedule ho chuka hai.",
            proposed_actions=[],
        )
    ]
    worker.process(inbound("in-3", "Is reminder ko Sunday shaam 6 baje reschedule kar do"))

    with database.session() as session:
        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        action = pending.payload["proposed_actions"][0]
        assert action["action_type"] == "reschedule_reminder"
        assert action["reminder_id"] == 1
        assert datetime.fromisoformat(action["scheduled_at"]).hour == 12  # 18:00 IST in UTC
    assert "naye samay" in _transport.sent[-1].text


def test_event_category_creates_configured_advance_reminder(tmp_path: Path) -> None:
    starts_at = datetime.now(timezone.utc) + timedelta(days=3)
    provider = FakeProvider(
        [
            AssistantDecision(
                intent="create_reminder",
                response="Appointment add kar doon?",
                proposed_actions=[
                    ProposedAction(
                        action_type="create_timeline_event",
                        title="Doctor appointment",
                        event_category="medical_appointment",
                        starts_at=starts_at.isoformat(),
                        ends_at=(starts_at + timedelta(hours=1)).isoformat(),
                    )
                ],
            ),
            confirmation_decision(),
        ]
    )
    worker, database, _transport = make_worker(tmp_path, provider)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        source = repository.add_inbound(user, inbound("pref", "one day before"))
        repository.set_preference(user.id, source.id, "medical_appointment_reminder_minutes", "1440")

    worker.process(inbound("in-1", "Doctor appointment add karo"))
    worker.process(inbound("in-2", "Haan"))

    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder.title == "Upcoming: Doctor appointment"
        assert reminder.due_at.replace(tzinfo=timezone.utc) == starts_at - timedelta(days=1)


class FakeCalendar:
    def __init__(self, fail_times: int = 0) -> None:
        self.created: list[tuple] = []
        self.updated: list[tuple] = []
        self.deleted: list[str] = []
        self.fail_times = fail_times

    def _maybe_fail(self) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("calendar unavailable")

    def create_event(self, title, starts_at, ends_at, timezone_name):
        self._maybe_fail()
        self.created.append((title, starts_at, ends_at, timezone_name))
        return f"google-event-{len(self.created)}"

    def update_event(self, event_id, title, starts_at, ends_at, timezone_name):
        self._maybe_fail()
        self.updated.append((event_id, title, starts_at, ends_at, timezone_name))

    def delete_event(self, event_id):
        self._maybe_fail()
        self.deleted.append(event_id)


def test_calendar_event_id_is_reused_for_reschedule_and_cancel(tmp_path: Path) -> None:
    calendar = FakeCalendar()
    original = datetime.now(timezone.utc) + timedelta(days=1)
    replacement = original + timedelta(hours=3)
    provider = FakeProvider([reminder_decision(original), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider, calendar)
    outbox = CalendarWorker(database, calendar, backoff_seconds=0)

    worker.process(inbound("cal-1", "Reminder set karo"))
    worker.process(inbound("cal-2", "Haan"))
    assert outbox.drain_once() == 1
    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder.calendar_event_id == "google-event-1"
        assert reminder.calendar_sync_status == "synced"
        reminder_id = reminder.id

    provider.decisions = [
        AssistantDecision(
            intent="reschedule_reminder",
            response="Reschedule kar doon?",
            proposed_actions=[
                ProposedAction(
                    action_type="reschedule_reminder",
                    reminder_id=reminder_id,
                    scheduled_at=replacement.isoformat(),
                )
            ],
        ),
        confirmation_decision(),
    ]
    worker.process(inbound("cal-3", "Time badal do"))
    worker.process(inbound("cal-4", "Haan"))
    outbox.drain_once()
    assert calendar.updated[0][0] == "google-event-1"
    assert len(calendar.created) == 1

    provider.decisions = [
        AssistantDecision(
            intent="cancel_reminder",
            response="Cancel kar doon?",
            proposed_actions=[ProposedAction(action_type="cancel_reminder", reminder_id=reminder_id)],
        ),
        confirmation_decision(),
    ]
    worker.process(inbound("cal-5", "Reminder cancel karo"))
    worker.process(inbound("cal-6", "Haan"))
    outbox.drain_once()
    assert calendar.deleted == ["google-event-1"]


def test_calendar_outage_does_not_block_or_lose_the_local_reminder(tmp_path: Path) -> None:
    """Local state commits regardless; the sync is retried until it lands."""
    calendar = FakeCalendar(fail_times=2)
    due_at = datetime.now(timezone.utc) + timedelta(days=1)
    provider = FakeProvider([reminder_decision(due_at), confirmation_decision()])
    worker, database, transport = make_worker(tmp_path, provider, calendar)
    outbox = CalendarWorker(database, calendar, backoff_seconds=0)

    worker.process(inbound("out-1", "Reminder set karo"))
    worker.process(inbound("out-2", "Haan"))

    # The user is told the reminder exists even though Calendar is down.
    assert transport.sent[-1].text.endswith("Reminder laga diya.")
    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder is not None and reminder.status == "pending"
        assert reminder.calendar_sync_status == "pending"

    assert outbox.drain_once() == 0  # first failure
    assert outbox.drain_once() == 0  # second failure
    assert outbox.drain_once() == 1  # recovers

    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder.calendar_event_id == "google-event-1"
        assert reminder.calendar_sync_status == "synced"
    assert len(calendar.created) == 1


def test_calendar_op_gives_up_and_is_marked_failed(tmp_path: Path) -> None:
    calendar = FakeCalendar(fail_times=99)
    due_at = datetime.now(timezone.utc) + timedelta(days=1)
    provider = FakeProvider([reminder_decision(due_at), confirmation_decision()])
    worker, database, _transport = make_worker(tmp_path, provider, calendar)
    outbox = CalendarWorker(database, calendar, max_attempts=3, backoff_seconds=0)

    worker.process(inbound("dead-1", "Reminder set karo"))
    worker.process(inbound("dead-2", "Haan"))
    for _ in range(3):
        outbox.drain_once()

    with database.session() as session:
        reminder = session.scalar(select(Reminder))
        assert reminder.calendar_sync_status == "failed"
        assert reminder.status == "pending"  # the reminder itself still works
        op = session.scalar(select(CalendarOp))
        assert op.status == "failed"
        assert "calendar unavailable" in op.last_error


def test_canonical_timeline_range_is_filled_when_model_omits_actions(tmp_path: Path) -> None:
    provider = FakeProvider(
        [AssistantDecision(intent="answer_question", response="Appointment add kar doon?")]
    )
    worker, database, _transport = make_worker(tmp_path, provider)

    worker.process(inbound("timeline-range", "Kal shaam 4:30 se 5:30 doctor appointment add karo"))

    with database.session() as session:
        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        action = pending.payload["proposed_actions"][0]
        start = datetime.fromisoformat(action["starts_at"])
        end = datetime.fromisoformat(action["ends_at"])
        ist = timezone(timedelta(hours=5, minutes=30))
        assert (start.astimezone(ist).hour, start.astimezone(ist).minute) == (16, 30)
        assert (end.astimezone(ist).hour, end.astimezone(ist).minute) == (17, 30)
        assert action["event_category"] == "medical_appointment"
