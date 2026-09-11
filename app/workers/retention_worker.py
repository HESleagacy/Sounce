from __future__ import annotations

import logging
from pathlib import Path
from threading import Event, Thread

from sqlalchemy import select

from app.persistence.database import Database
from app.persistence.models import User
from app.privacy import apply_retention

log = logging.getLogger(__name__)


class RetentionWorker:
    """Applies the retention policy on a slow schedule.

    Retention only touches raw material — messages, transcripts, media, settled
    jobs. Memories, reminders, preferences and timeline events are the product
    and are kept until the owner erases them.
    """

    def __init__(
        self,
        database: Database,
        retention_days: int,
        media_dir: Path,
        interval_hours: int = 24,
    ) -> None:
        self._database = database
        self._retention_days = retention_days
        self._media_dir = media_dir
        self._interval_seconds = max(1, interval_hours) * 3600
        self._stopping = Event()
        self._thread = Thread(target=self._run, name="retention-worker", daemon=True)

    @property
    def enabled(self) -> bool:
        return self._retention_days > 0

    def start(self) -> None:
        if not self.enabled:
            log.info("Retention is disabled (RETENTION_DAYS=0); raw messages are kept indefinitely")
            return
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return self._thread.is_alive() if self.enabled else True

    def run_once(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        with self._database.session() as session:
            user_ids = list(session.scalars(select(User.id)))
        for user_id in user_ids:
            with self._database.session() as session:
                counts = apply_retention(session, user_id, self._retention_days, self._media_dir)
            for key, value in counts.items():
                totals[key] = totals.get(key, 0) + value
        return totals

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.run_once()
            except Exception:
                log.exception("Retention pass failed")
            self._stopping.wait(self._interval_seconds)
