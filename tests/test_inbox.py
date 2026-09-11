"""The durable inbox: no accepted message is ever dropped."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.assistant.decision_engine import DecisionEngine
from app.assistant.schemas import AssistantDecision
from app.assistant.service import AssistantService
from app.domain.messages import InboundMessage, MessageType, OutboundMessage
from app.persistence.database import Database
from app.persistence.models import Base, InboundJob, Message
from app.persistence.repositories import Repository
from app.workers.message_worker import MessageWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"
STRANGER = "919000000000@s.whatsapp.net"


class EchoProvider:
    def interpret(self, message: str, _context: str) -> AssistantDecision:
        return AssistantDecision(intent="answer_question", response=f"ok: {message}")


class FakeTransport:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[OutboundMessage] = []
        self.fail = fail

    def set_message_handler(self, _handler: object) -> None:
        pass

    def send_text(self, chat_jid: str, text: str) -> OutboundMessage:
        if self.fail:
            raise RuntimeError("whatsapp down")
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


def inbound(message_id: str, text: str, sender: str = OWNER) -> InboundMessage:
    return InboundMessage(
        whatsapp_message_id=message_id,
        chat_jid=sender,
        sender_jid=sender,
        message_type=MessageType.TEXT,
        text=text,
        occurred_at=datetime.now(timezone.utc),
        is_from_me=True,
        is_self_chat=sender == OWNER,
    )


def make_worker(
    tmp_path: Path, queue_size: int = 100, transport: FakeTransport | None = None, **kwargs
) -> tuple[MessageWorker, Database, FakeTransport]:
    database = Database(f"sqlite:///{tmp_path / 'inbox.db'}")
    Base.metadata.create_all(database.engine)
    transport = transport or FakeTransport()
    assistant = AssistantService(EchoProvider(), DecisionEngine(pending_action_ttl_minutes=30))
    worker = MessageWorker(
        database,
        assistant,
        transport,
        OWNER,
        "Asia/Kolkata",
        media_dir=tmp_path / "media",
        queue_size=queue_size,
        **kwargs,
    )
    return worker, database, transport


def test_a_full_queue_does_not_lose_messages(tmp_path: Path) -> None:
    """The old behaviour logged an error and dropped the message. Now it persists."""
    worker, database, _transport = make_worker(tmp_path, queue_size=1)

    for index in range(5):
        worker.enqueue(inbound(f"in-{index}", f"message {index}"))

    # One made it into the in-memory queue; every one of them is durable.
    assert worker.queue_depth() == 1
    with database.session() as session:
        jobs = session.scalars(select(InboundJob)).all()
        assert len(jobs) == 5
        assert {job.status for job in jobs} == {"queued"}
    assert worker.backlog() == 5


def test_overflowed_messages_are_recovered_and_processed(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path, queue_size=1)
    for index in range(4):
        worker.enqueue(inbound(f"in-{index}", f"message {index}"))

    processed = 0
    for _ in range(10):
        worker.recover()
        with database.session() as session:
            pending = Repository(session).due_inbound_job_ids()
        if not pending:
            break
        for job_id in pending:
            if worker.process_job(job_id):
                processed += 1

    assert processed == 4
    assert worker.backlog() == 0
    assert len(transport.sent) == 4
    with database.session() as session:
        statuses = {job.status for job in session.scalars(select(InboundJob)).all()}
        assert statuses == {"done"}


def test_jobs_left_by_a_crashed_process_are_reclaimed(tmp_path: Path) -> None:
    """A job leased by a process that died must not stay stuck forever."""
    worker, database, _transport = make_worker(tmp_path, lease_seconds=10)
    worker.enqueue(inbound("in-crash", "hello"))
    with database.session() as session:
        job_id = Repository(session).due_inbound_job_ids()[0]
        # Simulate the claim, then the process dying before settling it.
        Repository(session).claim_inbound_job(job_id, lease_seconds=-1)

    with database.session() as session:
        assert session.get(InboundJob, job_id).status == "processing"

    assert worker.recover() == 1
    with database.session() as session:
        assert session.get(InboundJob, job_id).status == "queued"


def test_repeated_failures_dead_letter_and_tell_the_owner(tmp_path: Path) -> None:
    transport = FakeTransport(fail=True)
    worker, database, _t = make_worker(tmp_path, transport=transport, max_attempts=2, backoff_seconds=0)
    worker.enqueue(inbound("in-bad", "boom"))
    with database.session() as session:
        job_id = Repository(session).due_inbound_job_ids()[0]

    assert worker.process_job(job_id) is False
    with database.session() as session:
        assert session.get(InboundJob, job_id).status == "queued"  # retried, not lost

    assert worker.process_job(job_id) is False
    with database.session() as session:
        job = session.get(InboundJob, job_id)
        assert job.status == "dead"
        assert "whatsapp down" in job.last_error
    assert worker.dead_letters() == 1


def test_duplicates_and_strangers_never_enter_the_inbox(tmp_path: Path) -> None:
    worker, database, _transport = make_worker(tmp_path)

    worker.enqueue(inbound("in-1", "hello"))
    worker.enqueue(inbound("in-1", "hello"))  # same WhatsApp id
    worker.enqueue(inbound("in-2", "hello", sender=STRANGER))

    with database.session() as session:
        assert len(session.scalars(select(InboundJob)).all()) == 1


def test_a_processed_message_is_not_reprocessed_after_restart(tmp_path: Path) -> None:
    worker, database, transport = make_worker(tmp_path)
    worker.enqueue(inbound("in-restart", "hello"))
    with database.session() as session:
        job_id = Repository(session).due_inbound_job_ids()[0]
    assert worker.process_job(job_id) is True

    # A "restart": recovery must find nothing left to do.
    assert worker.recover() == 0
    assert worker.backlog() == 0
    assert len(transport.sent) == 1
    with database.session() as session:
        inbound_messages = [
            message for message in session.scalars(select(Message)).all() if message.direction == "inbound"
        ]
        assert len(inbound_messages) == 1
