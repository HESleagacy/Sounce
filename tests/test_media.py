from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.assistant.decision_engine import DecisionEngine
from app.assistant.schemas import AssistantDecision, AudioDecision, ProposedAction
from app.assistant.service import AssistantService
from app.domain.messages import InboundMessage, MessageType, OutboundMessage
from app.persistence.database import Database
from app.persistence.models import Base, Document, Message
from app.persistence.repositories import Repository
from app.providers.web import FetchedPage
from app.workers.message_worker import MessageWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


class FakeMediaProvider:
    def __init__(self, decision: AssistantDecision) -> None:
        self.decision = decision
        self.audio_calls: list[tuple[bytes, str]] = []
        self.document_calls: list[tuple[str, str, str | None]] = []
        self.localization_calls: list[tuple[str, str]] = []
        self.localized_response: str | None = None

    def interpret(self, _message: str, _context: str) -> AssistantDecision:
        raise AssertionError("text path should not be used")

    def interpret_audio(self, audio: bytes, mime_type: str, _context: str) -> AssistantDecision:
        self.audio_calls.append((audio, mime_type))
        return self.decision

    def localize_response(self, response: str, language: str) -> str:
        self.localization_calls.append((response, language))
        return self.localized_response or response

    def interpret_document(
        self, _data: bytes, mime_type: str, filename: str, caption: str | None, _context: str
    ) -> AssistantDecision:
        self.document_calls.append((mime_type, filename, caption))
        return self.decision


class FakeTransport:
    def __init__(self, media: bytes | None = b"media-bytes") -> None:
        self.sent: list[OutboundMessage] = []
        self.media = media
        self.voice_audio: list[bytes] = []

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
        self.voice_audio.append(audio)
        outbound = OutboundMessage(
            whatsapp_message_id=f"out-{len(self.sent) + 1}",
            chat_jid=chat_jid,
            text="[voice note]",
            occurred_at=datetime.now(timezone.utc),
        )
        self.sent.append(outbound)
        return outbound

    def download_media(self, whatsapp_message_id: str) -> bytes | None:
        return self.media

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass


def media_inbound(
    message_type: MessageType,
    mime_type: str,
    filename: str | None = None,
    size: int | None = 1024,
    caption: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        whatsapp_message_id="media-1",
        chat_jid=OWNER,
        sender_jid=OWNER,
        message_type=message_type,
        text=caption,
        occurred_at=datetime.now(timezone.utc),
        is_from_me=True,
        is_self_chat=True,
        media_mime_type=mime_type,
        media_filename=filename,
        media_size=size,
    )


def make_worker(
    tmp_path: Path,
    provider: FakeMediaProvider,
    transport: FakeTransport,
    web_fetcher=None,
    tts=None,
) -> tuple[MessageWorker, Database]:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(database.engine)
    assistant = AssistantService(provider, DecisionEngine(pending_action_ttl_minutes=30))
    worker = MessageWorker(
        database,
        assistant,
        transport,
        OWNER,
        "Asia/Kolkata",
        media_dir=tmp_path / "media",
        max_media_bytes=2048,
        tts=tts,
        web_fetcher=web_fetcher,
    )
    return worker, database


def test_voice_note_is_transcribed_and_stored_with_provenance(tmp_path: Path) -> None:
    decision = AssistantDecision(
        intent="answer_question",
        response="Samajh gaya.",
        transcript="Kal doctor ke paas jaana hai",
    )
    provider = FakeMediaProvider(decision)
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport)

    worker.process(media_inbound(MessageType.AUDIO, "audio/ogg; codecs=opus"))

    assert provider.audio_calls == [(b"media-bytes", "audio/ogg")]
    with database.session() as session:
        message = session.scalar(select(Message).where(Message.direction == "inbound"))
        assert message.transcript == "Kal doctor ke paas jaana hai"
        assert message.media_path is not None
        assert Path(message.media_path).read_bytes() == b"media-bytes"
    assert transport.sent[-1].text.endswith("Samajh gaya.")


def test_voice_note_gets_a_voice_reply_without_a_saved_preference(tmp_path: Path) -> None:
    decision = AudioDecision(
        intent="answer_question",
        response="Ho, mi aiktoy, okay.",
        transcript="Tu aiktoy ka? Yes?",
        detected_languages=["mr", "en"],
    )
    provider = FakeMediaProvider(decision)
    provider.localized_response = "हो, मी ऐकतोय."
    transport = FakeTransport()

    class FakeTTS:
        def synthesize(self, text: str, language: str | None = None) -> bytes:
            assert text == "हो, मी ऐकतोय."
            assert language == "mr"
            return b"ogg-opus"

    worker, _database = make_worker(tmp_path, provider, transport, tts=FakeTTS())

    worker.process(media_inbound(MessageType.AUDIO, "audio/ogg; codecs=opus"))

    assert provider.localization_calls == [("Ho, mi aiktoy, okay.", "mr")]
    assert transport.voice_audio == [b"ogg-opus"]
    assert transport.sent[-1].text == "[voice note]"


def test_document_is_summarized_and_stored_with_source_reference(tmp_path: Path) -> None:
    decision = AssistantDecision(
        intent="document_received",
        response="Yeh ek bijli ka bill lag raha hai. Iske saath kya karna hai?",
        document_summary="Electricity bill for July 2026",
        document_extracted_text="Amount due: Rs 2,340. Due date: 15 Aug 2026.",
        document_type="electricity_bill",
        document_dates=["2026-08-15"],
        document_amounts=["INR 2340"],
        document_entities=["Electricity provider"],
    )
    provider = FakeMediaProvider(decision)
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport)

    worker.process(media_inbound(MessageType.DOCUMENT, "application/pdf", filename="bill.pdf"))

    assert provider.document_calls == [("application/pdf", "bill.pdf", None)]
    with database.session() as session:
        document = session.scalar(select(Document))
        assert document is not None
        assert document.filename == "bill.pdf"
        assert document.summary == "Electricity bill for July 2026"
        assert "2,340" in document.extracted_text
        assert document.document_type == "electricity_bill"
        assert document.extracted_dates == ["2026-08-15"]
        assert document.extracted_amounts == ["INR 2340"]
        source = session.get(Message, document.source_message_id)
        assert source.whatsapp_message_id == "media-1"
    assert "Aap iske saath kya karna chahte hain?" in transport.sent[-1].text


def test_unsupported_mime_type_is_rejected_without_download(tmp_path: Path) -> None:
    provider = FakeMediaProvider(AssistantDecision(intent="answer_question", response="x"))
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport)

    worker.process(media_inbound(MessageType.DOCUMENT, "application/x-msdownload", filename="evil.exe"))

    assert provider.document_calls == []
    with database.session() as session:
        assert session.scalar(select(Document)) is None
    assert "supported nahi" in transport.sent[-1].text


def test_oversized_media_is_rejected(tmp_path: Path) -> None:
    provider = FakeMediaProvider(AssistantDecision(intent="answer_question", response="x"))
    transport = FakeTransport()
    worker, _database = make_worker(tmp_path, provider, transport)

    worker.process(
        media_inbound(MessageType.DOCUMENT, "application/pdf", filename="big.pdf", size=10_000_000)
    )

    assert provider.document_calls == []
    assert "badi hai" in transport.sent[-1].text


def test_failed_download_reports_error(tmp_path: Path) -> None:
    provider = FakeMediaProvider(AssistantDecision(intent="answer_question", response="x"))
    transport = FakeTransport(media=None)
    worker, _database = make_worker(tmp_path, provider, transport)

    worker.process(media_inbound(MessageType.AUDIO, "audio/ogg"))

    assert provider.audio_calls == []
    assert "download nahi" in transport.sent[-1].text


def test_uncaptioned_document_cannot_trigger_embedded_actions(tmp_path: Path) -> None:
    decision = AssistantDecision(
        intent="create_reminder",
        response="Reminder set kar doon?",
        document_summary="Document asks for a medicine reminder",
        proposed_actions=[
            ProposedAction(
                action_type="create_reminder",
                title="Take medicine",
                scheduled_at="2026-08-09T08:00:00+05:30",
            )
        ],
    )
    provider = FakeMediaProvider(decision)
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport)

    worker.process(media_inbound(MessageType.DOCUMENT, "application/pdf", filename="instructions.pdf"))

    with database.session() as session:
        from app.persistence.models import PendingAction, Reminder

        assert session.scalar(select(PendingAction)) is None
        assert session.scalar(select(Reminder)) is None
    assert "Aap iske saath kya karna chahte hain?" in transport.sent[-1].text


def test_public_link_is_summarized_and_source_linked(tmp_path: Path) -> None:
    class FakeWebFetcher:
        def fetch(self, url: str) -> FetchedPage:
            return FetchedPage(
                url=url,
                content=b"Electricity bill amount INR 4820 due 18 August",
                filename="example.com.txt",
            )

    provider = FakeMediaProvider(
        AssistantDecision(
            intent="document_received",
            response="Page summarized.",
            document_summary="Electricity bill",
            document_type="web_page",
            document_amounts=["INR 4820"],
            document_dates=["18 August"],
        )
    )
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport, FakeWebFetcher())

    worker.process(
        InboundMessage(
            whatsapp_message_id="link-1",
            chat_jid=OWNER,
            sender_jid=OWNER,
            message_type=MessageType.TEXT,
            text="https://example.com/bill",
            occurred_at=datetime.now(timezone.utc),
            is_from_me=True,
            is_self_chat=True,
        )
    )

    with database.session() as session:
        document = session.scalar(select(Document))
        assert document.storage_path == "https://example.com/bill"
        assert document.extracted_amounts == ["INR 4820"]
        assert session.get(Message, document.source_message_id).whatsapp_message_id == "link-1"
    assert "Aap iske saath kya karna chahte hain?" in transport.sent[-1].text


def test_document_context_lets_the_model_propose_a_confirmable_reminder(tmp_path: Path) -> None:
    """The bill's due date reaches the model through context, not through a regex.

    The assistant's job here is to put the stored document (and its extracted
    dates) in front of the model and then hold the model's proposal behind a
    confirmation. Deriving "do din pehle" from the document itself is the
    model's work, not a hardcoded shortcut.
    """
    provider = FakeMediaProvider(AssistantDecision(intent="answer_question", response="unused"))
    transport = FakeTransport()
    worker, database = make_worker(tmp_path, provider, transport)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(OWNER, "Asia/Kolkata")
        source = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id="bill-source",
                chat_jid=OWNER,
                sender_jid=OWNER,
                message_type=MessageType.DOCUMENT,
                text=None,
                occurred_at=datetime.now(timezone.utc),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        repository.add_document(
            user.id,
            source.id,
            "bill.pdf",
            "application/pdf",
            "data/media/bill.pdf",
            "Electricity bill",
            "Due 2026-09-18",
            document_type="electricity_bill",
            extracted_dates=["2026-09-18"],
            extracted_amounts=["INR 4820"],
        )

    seen_contexts: list[str] = []

    def interpret(_message: str, context: str) -> AssistantDecision:
        seen_contexts.append(context)
        return AssistantDecision(
            intent="create_reminder",
            response="Due date se do din pehle reminder laga doon?",
            proposed_actions=[
                ProposedAction(
                    action_type="create_reminder",
                    title="Electricity bill due soon",
                    scheduled_at="2026-09-16T09:00:00+05:30",
                    category="bill",
                )
            ],
        )

    provider.interpret = interpret
    worker.process(
        InboundMessage(
            whatsapp_message_id="bill-followup",
            chat_jid=OWNER,
            sender_jid=OWNER,
            message_type=MessageType.TEXT,
            text="Haan, do din pehle yaad dila dena",
            occurred_at=datetime.now(timezone.utc),
            is_from_me=True,
            is_self_chat=True,
        )
    )

    # The model was actually consulted, and it could see the bill.
    assert len(seen_contexts) == 1
    assert "2026-09-18" in seen_contexts[0]
    assert "electricity_bill" in seen_contexts[0]

    with database.session() as session:
        from app.persistence.models import PendingAction

        pending = session.scalar(select(PendingAction).where(PendingAction.status == "pending"))
        action = pending.payload["proposed_actions"][0]
        due_at = datetime.fromisoformat(action["scheduled_at"])
        assert due_at.date().isoformat() == "2026-09-16"
        assert action["category"] == "bill"
