from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def rowcount(result: Any) -> int:
    """Rows affected by a DML statement.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount``; only the ``CursorResult`` a DML statement actually returns
    does. This keeps the narrowing in one place instead of scattering casts.
    """
    return int(result.rowcount or 0)


def create_database_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Database:
    def __init__(self, database_url: str) -> None:
        self.engine = create_database_engine(database_url)
        self._sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
