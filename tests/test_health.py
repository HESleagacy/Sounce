"""Liveness and readiness probes actually check things."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from app.health import HealthProbes, start_health_server
from app.main import run_migrations
from app.persistence.database import Database
from app.persistence.models import Base

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


class StubWorker:
    def __init__(self, alive: bool = True, depth: int = 0, backlog: int = 0, dead: int = 0) -> None:
        self._alive = alive
        self._depth = depth
        self._backlog = backlog
        self._dead = dead

    def is_alive(self) -> bool:
        return self._alive

    def queue_depth(self) -> int:
        return self._depth

    def backlog(self) -> int:
        return self._backlog

    def dead_letters(self) -> int:
        return self._dead


class StubTransport:
    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


def make_probes(tmp_path: Path, migrated: bool = True, **kwargs) -> HealthProbes:
    url = f"sqlite:///{tmp_path / 'health.db'}"
    if migrated:
        run_migrations(url)
        database = Database(url)
    else:
        database = Database(url)
        Base.metadata.create_all(database.engine)
    defaults = {
        "database": database,
        "alembic_ini": ALEMBIC_INI,
        "message_worker": StubWorker(),
        "reminder_worker": StubWorker(),
        "calendar_worker": StubWorker(),
        "transport": StubTransport(),
        "provider_configured": True,
    }
    defaults.update(kwargs)
    return HealthProbes(**defaults)


def test_readiness_reports_each_dependency(tmp_path: Path) -> None:
    probes = make_probes(tmp_path)
    _ready, payload = probes.check()

    assert set(payload["checks"]) == {
        "database",
        "migrations",
        "workers",
        "queue",
        "whatsapp",
        "decision_provider",
    }
    assert payload["checks"]["database"]["ok"] is True


def test_a_dead_worker_makes_the_service_not_ready(tmp_path: Path) -> None:
    probes = make_probes(tmp_path, reminder_worker=StubWorker(alive=False))
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["workers"]["ok"] is False
    assert payload["checks"]["workers"]["detail"]["reminder"] is False


def test_a_disconnected_whatsapp_makes_the_service_not_ready(tmp_path: Path) -> None:
    probes = make_probes(tmp_path, transport=StubTransport(connected=False))
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["whatsapp"]["detail"] == "disconnected"


def test_an_undrained_backlog_makes_the_service_not_ready(tmp_path: Path) -> None:
    probes = make_probes(tmp_path, message_worker=StubWorker(backlog=900, dead=4), backlog_threshold=500)
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["queue"]["durable_backlog"] == 900
    assert payload["checks"]["queue"]["dead_lettered"] == 4


def test_a_database_behind_head_makes_the_service_not_ready(tmp_path: Path) -> None:
    """An unmigrated schema must take the instance out of rotation, not serve traffic."""
    probes = make_probes(tmp_path, migrated=False)
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["migrations"]["ok"] is False
    assert payload["checks"]["migrations"]["current"] is None
    assert payload["checks"]["migrations"]["head"] is not None


def test_a_missing_api_key_makes_the_service_not_ready(tmp_path: Path) -> None:
    probes = make_probes(tmp_path, provider_configured=False)
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["decision_provider"]["detail"] == "missing API key"


def test_an_unreachable_database_makes_the_service_not_ready(tmp_path: Path) -> None:
    probes = make_probes(tmp_path)
    probes.database.dispose()
    probes.database = Database("sqlite:////nonexistent-directory/health.db")
    ready, payload = probes.check()

    assert ready is False
    assert payload["checks"]["database"]["ok"] is False


def _get(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_live_and_ready_are_separate_endpoints(tmp_path: Path) -> None:
    probes = make_probes(tmp_path, transport=StubTransport(connected=False))
    server = start_health_server(0, probes)
    port = server.server_address[1]
    try:
        status, payload = _get(port, "/live")
        assert (status, payload["status"]) == (200, "live")
        assert payload["detail"] == "reconnecting"
        # Not ready, but still alive: take it out of rotation, do not restart it.
        status, payload = _get(port, "/ready")
        assert status == 503
        assert payload["status"] == "not_ready"
        assert _get(port, "/nope")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_ready_returns_200_when_everything_is_healthy(tmp_path: Path) -> None:
    probes = make_probes(tmp_path)
    server = start_health_server(0, probes)
    port = server.server_address[1]
    try:
        status, payload = _get(port, "/ready")
        assert (status, payload["status"]) == (200, "ready")
    finally:
        server.shutdown()
        server.server_close()


def test_live_fails_once_a_disconnect_outlasts_the_grace_period(tmp_path: Path) -> None:
    """A wedged connect() leaves the process disconnected forever; /live must say so."""
    probes = make_probes(tmp_path, transport=StubTransport(connected=False), stall_seconds=0)
    alive, payload = probes.liveness()
    assert (alive, payload["status"]) == (False, "stalled")


def test_live_recovers_and_rearms_after_reconnecting(tmp_path: Path) -> None:
    transport = StubTransport(connected=False)
    probes = make_probes(tmp_path, transport=transport, stall_seconds=0)
    assert probes.liveness()[0] is False

    transport._connected = True
    assert probes.liveness()[0] is True

    # The stall clock restarts from the new disconnect, not the original one.
    transport._connected = False
    probes.stall_seconds = 3600
    alive, payload = probes.liveness()
    assert (alive, payload["detail"]) == (True, "reconnecting")
