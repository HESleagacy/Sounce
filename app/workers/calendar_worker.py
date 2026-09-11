from __future__ import annotations

import logging
from datetime import datetime
from threading import Event, Thread

from app.assistant.decision_engine import CalendarSync
from app.persistence.database import Database
from app.persistence.models import CalendarOp
from app.persistence.repositories import Repository

log = logging.getLogger(__name__)


class CalendarWorker:
    """Drains the Calendar outbox.

    Ops are applied in insertion order, so a create always lands before the
    update or delete that follows it. Update and delete read the remote event id
    from the entity at drain time rather than from the op payload, which is what
    lets a reschedule be queued while its create is still in flight.
    """

    def __init__(
        self,
        database: Database,
        calendar: CalendarSync,
        poll_seconds: int = 15,
        max_attempts: int = 6,
        backoff_seconds: int = 30,
    ) -> None:
        self._database = database
        self._calendar = calendar
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._stopping = Event()
        self._thread = Thread(target=self._run, name="calendar-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def drain_once(self) -> int:
        """Apply every op that is due right now. Returns how many synced."""
        with self._database.session() as session:
            op_ids = Repository(session).due_calendar_op_ids()
        synced = 0
        for op_id in op_ids:
            if self._apply(op_id):
                synced += 1
        return synced

    def _apply(self, op_id: int) -> bool:
        with self._database.session() as session:
            repository = Repository(session)
            record = session.get(CalendarOp, op_id)
            if record is None or record.status != "pending":
                return False
            entity = repository.calendar_entity(record)
            if entity is None:
                log.warning("Calendar op %s references a missing %s", op_id, record.entity_type)
                repository.mark_calendar_op_failed(op_id, "entity missing", 1, self._backoff_seconds)
                return False
            event_id = entity.calendar_event_id
            if record.op in {"update", "delete"} and not event_id:
                # The create either has not drained yet or failed for good.
                if entity.calendar_sync_status == "failed":
                    repository.mark_calendar_op_failed(
                        op_id, "no remote event to modify", 1, self._backoff_seconds
                    )
                else:
                    repository.mark_calendar_op_failed(
                        op_id, "waiting for create", self._max_attempts, self._backoff_seconds
                    )
                return False
            payload = record.payload
            try:
                new_event_id = self._call(record.op, event_id, payload)
            except Exception as exc:
                outcome = repository.mark_calendar_op_failed(
                    op_id, repr(exc), self._max_attempts, self._backoff_seconds
                )
                log.warning(
                    "Calendar op %s (%s %s) failed and is now %s: %s",
                    op_id,
                    record.op,
                    record.entity_type,
                    outcome,
                    exc,
                )
                return False
            repository.mark_calendar_op_synced(op_id, new_event_id)
            log.info("Calendar op %s (%s %s) synced", op_id, record.op, record.entity_type)
            return True

    def _call(self, op: str, event_id: str | None, payload: dict[str, object]) -> str | None:
        if op == "create":
            return self._calendar.create_event(
                str(payload["title"]),
                datetime.fromisoformat(str(payload["starts_at"])),
                datetime.fromisoformat(str(payload["ends_at"])),
                str(payload["timezone"]),
            )
        if op == "update":
            self._calendar.update_event(
                str(event_id),
                str(payload["title"]),
                datetime.fromisoformat(str(payload["starts_at"])),
                datetime.fromisoformat(str(payload["ends_at"])),
                str(payload["timezone"]),
            )
            return event_id
        if op == "delete":
            self._calendar.delete_event(str(event_id))
            return event_id
        raise ValueError(f"Unknown calendar op: {op!r}")

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.drain_once()
            except Exception:
                log.exception("Calendar outbox drain cycle failed")
            self._stopping.wait(self._poll_seconds)
