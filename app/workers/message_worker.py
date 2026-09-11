from __future__ import annotations

import logging
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread

import httpx

from app.assistant.service import AssistantService
from app.domain.messages import InboundMessage, MessageType, inbound_from_payload
from app.persistence.database import Database
from app.persistence.models import Message, User
from app.persistence.repositories import Repository
from app.privacy import purge_user_data
from app.providers.maya import TTSProvider
from app.providers.web import SafeWebFetcher, first_url
from app.transport.base import MessageTransport

log = logging.getLogger(__name__)

DOCUMENT_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "text/plain",
}
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "text/plain": ".txt",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}
FALLBACK_ERROR_TEXT = "Kuch gadbad ho gayi, thodi der baad dobara koshish karein."
TOO_LARGE_TEXT = "Yeh file bahut badi hai, isliye main ise process nahi kar paya."
UNSUPPORTED_TYPE_TEXT = "Is tarah ki file abhi supported nahi hai. PDF, image, ya text file bhejein."
DOWNLOAD_FAILED_TEXT = "File download nahi ho payi, dobara bhejne ki koshish karein."
PURGED_TEXT = "Aapka saara data delete kar diya gaya hai."
QUEUE_POLL_SECONDS = 0.5
# Sweep the durable inbox after roughly ten idle seconds, which is how work
# left behind by a full queue or a crashed process gets picked back up.
RECOVERY_IDLE_CYCLES = 20


def format_assistant_response(text: str) -> str:
    return f"*Sounce*\n{text.strip()}"


class MessageWorker:
    """Processes inbound WhatsApp messages from a durable inbox.

    Every accepted message is committed to ``inbound_jobs`` before it is handed
    to the in-memory queue. The queue is only there to keep latency low; if it
    is full, or the process dies, the job is still in the database and the
    recovery sweep picks it up. Nothing the owner sends is ever dropped.
    """

    def __init__(
        self,
        database: Database,
        assistant: AssistantService,
        transport: MessageTransport,
        owner_jid: str,
        owner_timezone: str,
        media_dir: Path = Path("data/media"),
        max_media_bytes: int = 20 * 1024 * 1024,
        tts: TTSProvider | None = None,
        web_fetcher: SafeWebFetcher | None = None,
        queue_size: int = 100,
        lease_seconds: int = 300,
        max_attempts: int = 3,
        backoff_seconds: int = 10,
    ) -> None:
        self._database = database
        self._assistant = assistant
        self._transport = transport
        self._owner_jid = owner_jid
        self._owner_timezone = owner_timezone
        self._media_dir = media_dir
        self._max_media_bytes = max_media_bytes
        self._tts = tts
        self._web_fetcher = web_fetcher
        self._queue: Queue[int] = Queue(maxsize=queue_size)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._stopping = Event()
        self._thread = Thread(target=self._run, name="message-worker", daemon=True)

    def enqueue(self, message: InboundMessage) -> None:
        """Commit the message, then try to hand it to the queue.

        A full queue is back-pressure, not data loss: the job is already durable
        and ``recover()`` will pick it up on the next idle cycle.
        """
        if not self._is_owner_message(message):
            log.warning("Ignoring message outside the configured owner self-chat")
            return
        with self._database.session() as session:
            repository = Repository(session)
            if repository.has_message(message.whatsapp_message_id):
                log.info("Ignoring duplicate or known message %s", message.whatsapp_message_id)
                return
            job = repository.enqueue_inbound_job(message)
            if job is None:
                log.info("Message %s is already in the inbox", message.whatsapp_message_id)
                return
            job_id = job.id
        try:
            self._queue.put_nowait(job_id)
        except Full:
            log.warning(
                "Message queue is full; job %s stays in the inbox for the recovery sweep",
                job_id,
            )

    def start(self) -> None:
        self.recover()
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def backlog(self) -> int:
        """Jobs still waiting in the durable inbox, whatever the queue looks like."""
        with self._database.session() as session:
            return Repository(session).count_inbound_jobs("queued")

    def dead_letters(self) -> int:
        with self._database.session() as session:
            return Repository(session).count_inbound_jobs("dead")

    def recover(self) -> int:
        """Re-queue work left behind by a full queue or a previous process."""
        with self._database.session() as session:
            repository = Repository(session)
            reclaimed = repository.reclaim_expired_inbound_jobs()
            job_ids = repository.due_inbound_job_ids()
        if reclaimed:
            log.warning("Reclaimed %s inbound job(s) from an expired lease", reclaimed)
        queued = 0
        for job_id in job_ids:
            try:
                self._queue.put_nowait(job_id)
                queued += 1
            except Full:
                break
        if queued:
            log.info("Recovered %s inbound job(s) from the durable inbox", queued)
        return queued

    def process_job(self, job_id: int) -> bool:
        """Claim, process, and settle one inbox job."""
        with self._database.session() as session:
            repository = Repository(session)
            job = repository.claim_inbound_job(job_id, self._lease_seconds)
            if job is None:
                return False
            payload = dict(job.payload)
        try:
            inbound = inbound_from_payload(payload)
        except (KeyError, ValueError) as exc:
            log.exception("Inbox job %s has an unreadable payload", job_id)
            with self._database.session() as session:
                Repository(session).fail_inbound_job(job_id, repr(exc), 0, self._backoff_seconds)
            return False
        try:
            self.process(inbound)
        except Exception as exc:
            log.exception("Failed to process WhatsApp message %s", inbound.whatsapp_message_id)
            with self._database.session() as session:
                outcome = Repository(session).fail_inbound_job(
                    job_id, repr(exc), self._max_attempts, self._backoff_seconds
                )
            if outcome == "dead":
                log.error("Inbox job %s dead-lettered after %s attempts", job_id, self._max_attempts)
                self._notify_failure(inbound)
            return False
        with self._database.session() as session:
            Repository(session).finish_inbound_job(job_id)
        return True

    def _is_owner_message(self, inbound: InboundMessage) -> bool:
        return inbound.is_self_chat and inbound.sender_jid == self._owner_jid

    def _notify_failure(self, inbound: InboundMessage) -> None:
        """Tell the owner when a message is given up on, rather than failing silently."""
        try:
            with self._database.session() as session:
                repository = Repository(session)
                user = repository.get_or_create_user(inbound.sender_jid, self._owner_timezone)
                outbound = self._transport.send_text(
                    inbound.chat_jid, format_assistant_response(FALLBACK_ERROR_TEXT)
                )
                repository.add_outbound(user, outbound)
        except Exception:
            log.exception("Could not notify the owner about dead-lettered message")

    def process(self, inbound: InboundMessage) -> bool:
        if not self._is_owner_message(inbound):
            log.warning("Ignoring message outside the configured owner self-chat")
            return False
        with self._database.session() as session:
            repository = Repository(session)
            if repository.has_message(inbound.whatsapp_message_id):
                log.info("Ignoring duplicate or known outbound message %s", inbound.whatsapp_message_id)
                return False
            user = repository.get_or_create_user(inbound.sender_jid, self._owner_timezone)
            repository.update_chat_jid(user, inbound.chat_jid)
            source_message = repository.add_inbound(user, inbound)
            if source_message is None:
                return False
            purge_requested = False
            try:
                response_text, purge_requested = self._handle(inbound, repository, user, source_message)
            except Exception:
                log.exception("Failed to interpret WhatsApp message %s", inbound.whatsapp_message_id)
                response_text = FALLBACK_ERROR_TEXT
            if response_text is None:
                return True
            self._reply(
                repository,
                user,
                inbound.chat_jid,
                response_text,
                prefer_voice=inbound.message_type == MessageType.AUDIO,
                detected_languages=(
                    source_message.detected_languages if inbound.message_type == MessageType.AUDIO else None
                ),
            )
            user_id = user.id
        if purge_requested:
            self._purge(user_id, inbound.chat_jid)
        return True

    def _purge(self, user_id: int, chat_jid: str) -> None:
        """Erase everything, then confirm from a clean slate.

        Runs after the confirmation reply has been committed, because the purge
        removes the very rows that reply was written against.
        """
        with self._database.session() as session:
            counts = purge_user_data(session, user_id, self._media_dir)
        log.warning("Owner-requested erase completed: %s", counts)
        with self._database.session() as session:
            repository = Repository(session)
            user = repository.get_or_create_user(self._owner_jid, self._owner_timezone)
            repository.update_chat_jid(user, chat_jid)
            outbound = self._transport.send_text(chat_jid, format_assistant_response(PURGED_TEXT))
            repository.add_outbound(user, outbound)

    def _handle(
        self,
        inbound: InboundMessage,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> tuple[str | None, bool]:
        """Returns the reply text and whether a full erase was confirmed."""
        if inbound.message_type == MessageType.TEXT and inbound.text:
            url = first_url(inbound.text)
            if url and self._web_fetcher is not None:
                return self._handle_link(url, inbound, repository, user, source_message), False
            result = self._assistant.handle_text(inbound.text, repository, user, source_message)
            return result.response, result.purge_requested
        if inbound.message_type == MessageType.AUDIO:
            return self._handle_audio(inbound, repository, user, source_message), False
        if inbound.message_type in (MessageType.DOCUMENT, MessageType.IMAGE):
            return self._handle_document(inbound, repository, user, source_message), False
        return None, False

    def _handle_audio(
        self,
        inbound: InboundMessage,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> str:
        if inbound.media_size and inbound.media_size > self._max_media_bytes:
            return TOO_LARGE_TEXT
        audio = self._transport.download_media(inbound.whatsapp_message_id)
        if audio is None:
            return DOWNLOAD_FAILED_TEXT
        if len(audio) > self._max_media_bytes:
            return TOO_LARGE_TEXT
        mime_type = _base_mime(inbound.media_mime_type) or "audio/ogg"
        source_message.media_path = self._store_media(inbound.whatsapp_message_id, mime_type, audio)
        result = self._assistant.handle_audio(audio, mime_type, repository, user, source_message)
        if result.transcript:
            source_message.transcript = result.transcript
        source_message.detected_languages = result.detected_languages
        return result.execution.response

    def _handle_link(
        self,
        url: str,
        inbound: InboundMessage,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> str:
        if self._web_fetcher is None:
            return "Link padhne ki suvidha abhi enabled nahi hai."
        try:
            page = self._web_fetcher.fetch(url)
        except (ValueError, httpx.HTTPError, OSError) as exc:
            log.warning("Link fetch rejected for %s: %s", url, exc)
            return "Yeh link safely open nahi ho paya. Public text ya HTML link dobara bhejein."
        result = self._assistant.handle_document(
            page.content,
            "text/plain",
            page.filename,
            None,
            repository,
            user,
            source_message,
        )
        repository.add_document(
            user_id=user.id,
            source_message_id=source_message.id,
            filename=page.filename,
            mime_type="text/html",
            storage_path=page.url,
            summary=result.document_summary,
            extracted_text=result.document_extracted_text,
            document_type=result.document_type or "web_page",
            extracted_dates=result.document_dates,
            extracted_amounts=result.document_amounts,
            extracted_entities=result.document_entities,
        )
        return result.execution.response

    def _handle_document(
        self,
        inbound: InboundMessage,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> str:
        mime_type = _base_mime(inbound.media_mime_type)
        if mime_type not in DOCUMENT_MIME_TYPES:
            return UNSUPPORTED_TYPE_TEXT
        if inbound.media_size and inbound.media_size > self._max_media_bytes:
            return TOO_LARGE_TEXT
        data = self._transport.download_media(inbound.whatsapp_message_id)
        if data is None:
            return DOWNLOAD_FAILED_TEXT
        if len(data) > self._max_media_bytes:
            return TOO_LARGE_TEXT
        default_name = f"{inbound.whatsapp_message_id}{MIME_EXTENSIONS.get(mime_type, '')}"
        filename = inbound.media_filename or default_name
        storage_path = self._store_media(inbound.whatsapp_message_id, mime_type, data)
        source_message.media_path = storage_path
        result = self._assistant.handle_document(
            data, mime_type, filename, inbound.text, repository, user, source_message
        )
        repository.add_document(
            user_id=user.id,
            source_message_id=source_message.id,
            filename=filename,
            mime_type=mime_type,
            storage_path=storage_path,
            summary=result.document_summary,
            extracted_text=result.document_extracted_text,
            document_type=result.document_type,
            extracted_dates=result.document_dates,
            extracted_amounts=result.document_amounts,
            extracted_entities=result.document_entities,
        )
        return result.execution.response

    def _reply(
        self,
        repository: Repository,
        user: User,
        chat_jid: str,
        text: str,
        prefer_voice: bool = False,
        detected_languages: list[str] | None = None,
    ) -> None:
        formatted = format_assistant_response(text)
        preferences = repository.get_preferences(user.id)
        if (prefer_voice or preferences.get("response_modality") == "voice") and self._tts is not None:
            language = preferences.get("preferred_language")
            if detected_languages:
                language = detected_languages[0]
            audio = self._tts.synthesize(text, language)
            if audio is not None:
                try:
                    outbound = self._transport.send_voice_note(chat_jid, audio)
                    repository.add_outbound(user, outbound)
                    return
                except Exception:
                    log.exception("Voice note send failed; falling back to text")
        outbound = self._transport.send_text(chat_jid, formatted)
        repository.add_outbound(user, outbound)

    def _store_media(self, message_id: str, mime_type: str, data: bytes) -> str:
        self._media_dir.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(char for char in message_id if char.isalnum())
        path = self._media_dir / f"{safe_id}{MIME_EXTENSIONS.get(mime_type, '.bin')}"
        path.write_bytes(data)
        return str(path)

    def _run(self) -> None:
        idle_cycles = 0
        while not self._stopping.is_set():
            try:
                job_id = self._queue.get(timeout=QUEUE_POLL_SECONDS)
            except Empty:
                idle_cycles += 1
                if idle_cycles >= RECOVERY_IDLE_CYCLES:
                    idle_cycles = 0
                    try:
                        self.recover()
                    except Exception:
                        log.exception("Inbox recovery sweep failed")
                continue
            idle_cycles = 0
            try:
                self.process_job(job_id)
            except Exception:
                log.exception("Failed to settle inbox job %s", job_id)
            finally:
                self._queue.task_done()


def _base_mime(mime_type: str | None) -> str | None:
    if not mime_type:
        return None
    return mime_type.split(";", 1)[0].strip().lower()
