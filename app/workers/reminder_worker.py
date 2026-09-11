from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.domain.preferences import next_allowed_time
from app.domain.reminders import as_utc
from app.persistence.database import Database, rowcount
from app.persistence.models import Reminder, User
from app.persistence.repositories import Repository
from app.transport.base import MessageTransport
from app.workers.message_worker import format_assistant_response

log = logging.getLogger(__name__)


class ReminderWorker:
    """Delivers due reminders with a durable lease and a per-occurrence ledger.

    Delivery semantics
    ------------------
    Sending a WhatsApp message is an external side effect that cannot be
    enrolled in the database transaction, so true exactly-once is not available.
    What this worker guarantees instead:

    * An occurrence whose delivery was **committed** is never sent again. The
      unique ``(reminder_id, occurrence_key)`` row is the guard.
    * A crash in the window between the send and its commit costs **at most one**
      duplicate, and the attempt budget caps it.
    * A crash anywhere else costs nothing: the lease expires, the reminder
      returns to ``pending``, and it is retried.

    A missed reminder is worse than a repeated one for this product, so the
    ambiguous window resolves towards resending.
    """

    def __init__(
        self,
        database: Database,
        transport: MessageTransport,
        owner_jid: str,
        poll_seconds: int = 10,
        lease_seconds: int = 120,
        max_attempts: int = 5,
    ) -> None:
        self._database = database
        self._transport = transport
        self._owner_jid = owner_jid
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._stopping = Event()
        self._thread = Thread(target=self._run, name="reminder-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def reclaim_expired_leases(self) -> int:
        """Return reminders whose worker died mid-delivery to the pending pool."""
        now = datetime.now(timezone.utc)
        with self._database.session() as session:
            released = session.execute(
                update(Reminder)
                .where(
                    Reminder.status == "delivering",
                    Reminder.lease_expires_at.is_not(None),
                    Reminder.lease_expires_at < now,
                )
                .values(status="pending", lease_expires_at=None)
            )
            count = rowcount(released)
        if count:
            log.warning("Reclaimed %s reminder(s) from an expired delivery lease", count)
        return count

    def deliver_due_reminders(self) -> int:
        self.reclaim_expired_leases()
        now = datetime.now(timezone.utc)
        with self._database.session() as session:
            due_ids = [
                reminder.id
                for reminder in session.scalars(select(Reminder).where(Reminder.status == "pending"))
                if as_utc(reminder.due_at) <= now
            ]
        delivered = 0
        for reminder_id in due_ids:
            if self._deliver_one(reminder_id):
                delivered += 1
        return delivered

    def _deliver_one(self, reminder_id: int) -> bool:
        claim = self._claim(reminder_id)
        if claim is None:
            return False
        occurrence_key, chat_jid, title = claim

        try:
            outbound = self._transport.send_text(chat_jid, format_assistant_response(f"Reminder: {title}"))
        except Exception as exc:
            log.exception("Reminder %s delivery failed; will retry", reminder_id)
            with self._database.session() as session:
                repository = Repository(session)
                repository.fail_delivery(reminder_id, occurrence_key, repr(exc))
                reminder = session.get(Reminder, reminder_id)
                if reminder is not None:
                    reminder.status = "pending"
                    reminder.lease_expires_at = None
                    reminder.last_error = repr(exc)[:2000]
            return False

        with self._database.session() as session:
            repository = Repository(session)
            repository.complete_delivery(reminder_id, occurrence_key)
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                return True
            user = session.get(User, reminder.user_id)
            if user is not None:
                repository.add_outbound(user, outbound)
            reminder.delivered_at = datetime.now(timezone.utc)
            reminder.lease_expires_at = None
            reminder.last_error = None
            self._advance(reminder)
            log.info("Delivered reminder %s: %s", reminder_id, reminder.title)
        return True

    def _claim(self, reminder_id: int) -> tuple[str, str, str] | None:
        """Take a durable lease and open a delivery attempt.

        Everything here commits before a single byte goes to WhatsApp, which is
        what makes the ledger meaningful after a crash.
        """
        now = datetime.now(timezone.utc)
        with self._database.session() as session:
            claimed = session.execute(
                update(Reminder)
                .where(Reminder.id == reminder_id, Reminder.status == "pending")
                .values(
                    status="delivering",
                    lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                    delivery_attempts=Reminder.delivery_attempts + 1,
                )
            )
            if rowcount(claimed) != 1:
                return None
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                return None
            repository = Repository(session)
            user = session.get(User, reminder.user_id)
            preferences = repository.get_preferences(reminder.user_id)

            deferred_to = self._quiet_hours_deferral(reminder, preferences)
            if deferred_to is not None:
                # Deferral is not an attempt: undo the claim rather than burning
                # the reminder's delivery budget on a quiet-hours bounce.
                reminder.due_at = deferred_to
                reminder.status = "pending"
                reminder.lease_expires_at = None
                reminder.delivery_attempts = max(0, reminder.delivery_attempts - 1)
                log.info("Reminder %s deferred to %s by quiet hours", reminder_id, deferred_to)
                return None

            occurrence_key = as_utc(reminder.due_at).isoformat()
            outcome = repository.begin_delivery(reminder_id, occurrence_key, self._max_attempts)
            if outcome != "proceed":
                if outcome == "already_sent":
                    log.info(
                        "Reminder %s occurrence %s was already delivered; skipping",
                        reminder_id,
                        occurrence_key,
                    )
                    reminder.delivered_at = reminder.delivered_at or datetime.now(timezone.utc)
                    reminder.lease_expires_at = None
                    self._advance(reminder)
                elif outcome == "dead":
                    log.error(
                        "Reminder %s exhausted its delivery attempts; marking undeliverable",
                        reminder_id,
                    )
                    reminder.status = "undeliverable"
                    reminder.lease_expires_at = None
                else:
                    reminder.status = "pending"
                    reminder.lease_expires_at = None
                return None

            chat_jid = (user.chat_jid or user.whatsapp_jid) if user is not None else self._owner_jid
            return occurrence_key, chat_jid, reminder.title

    def _advance(self, reminder: Reminder) -> None:
        """Move a recurring reminder to its next firing, or close it out."""
        if reminder.recurrence_frequency:
            reminder.due_at = self._next_occurrence(reminder)
            reminder.status = "pending"
        else:
            reminder.status = "delivered"

    @staticmethod
    def _next_occurrence(reminder: Reminder) -> datetime:
        due_at = as_utc(reminder.due_at)
        interval = max(1, reminder.recurrence_interval or 1)
        now = datetime.now(timezone.utc)
        while due_at <= now:
            if reminder.recurrence_frequency == "daily":
                due_at += timedelta(days=interval)
            elif reminder.recurrence_frequency == "weekly":
                due_at += timedelta(weeks=interval)
            elif reminder.recurrence_frequency == "monthly":
                month_index = due_at.month - 1 + interval
                year = due_at.year + month_index // 12
                month = month_index % 12 + 1
                day = min(due_at.day, monthrange(year, month)[1])
                due_at = due_at.replace(year=year, month=month, day=day)
            else:
                return due_at
        return due_at

    @staticmethod
    def _quiet_hours_deferral(reminder: Reminder, preferences: dict[str, str]) -> datetime | None:
        timezone_name = reminder.timezone or "UTC"
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
        prefix = f"{reminder.category}_" if reminder.category else ""
        allowed_local = next_allowed_time(
            now_local,
            preferences.get(f"{prefix}quiet_hours_start") or preferences.get("quiet_hours_start"),
            preferences.get(f"{prefix}quiet_hours_end") or preferences.get("quiet_hours_end"),
        )
        if allowed_local is None:
            return None
        return allowed_local.astimezone(timezone.utc)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.deliver_due_reminders()
            except Exception:
                log.exception("Reminder polling cycle failed")
            self._stopping.wait(self._poll_seconds)
