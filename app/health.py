"""Liveness and readiness probes.

The previous health endpoint returned ``200 ok`` to any GET without looking at
anything, which meant an orchestrator would keep a container in rotation while
the database was unreachable, migrations were behind, the workers were dead, or
WhatsApp was disconnected. These probes actually check.

``/live`` answers "is the process running" — a failure here should restart the
container. ``/ready`` answers "can it do its job right now" — a failure here
should take it out of rotation without restarting it, because a WhatsApp
reconnect or a drained backlog fixes itself.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Protocol

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.persistence.database import Database

log = logging.getLogger(__name__)


class _Worker(Protocol):
    def is_alive(self) -> bool: ...


@dataclass
class HealthProbes:
    """Everything the readiness check is allowed to look at."""

    database: Database
    alembic_ini: Path
    message_worker: Any = None
    reminder_worker: _Worker | None = None
    calendar_worker: _Worker | None = None
    transport: Any = None
    provider_configured: bool = False
    backlog_threshold: int = 500
    _migration_head: str | None = field(default=None, repr=False)

    def check(self) -> tuple[bool, dict[str, Any]]:
        checks: dict[str, Any] = {}
        checks["database"] = self._database_check()
        checks["migrations"] = self._migration_check()
        checks["workers"] = self._worker_check()
        checks["queue"] = self._queue_check()
        checks["whatsapp"] = self._whatsapp_check()
        checks["decision_provider"] = {
            "ok": self.provider_configured,
            "detail": "configured" if self.provider_configured else "missing API key",
        }
        ready = all(item["ok"] for item in checks.values())
        return ready, {"status": "ready" if ready else "not_ready", "checks": checks}

    def _database_check(self) -> dict[str, Any]:
        try:
            with self.database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            return {"ok": False, "detail": f"unreachable: {exc.__class__.__name__}"}
        return {"ok": True, "detail": "reachable"}

    def _migration_check(self) -> dict[str, Any]:
        try:
            if self._migration_head is None:
                script = ScriptDirectory.from_config(Config(str(self.alembic_ini)))
                self._migration_head = script.get_current_head()
            with self.database.engine.connect() as connection:
                current = MigrationContext.configure(connection).get_current_revision()
        except Exception as exc:
            return {"ok": False, "detail": f"unknown: {exc.__class__.__name__}"}
        ok = current == self._migration_head
        return {
            "ok": ok,
            "detail": "at head" if ok else "behind",
            "current": current,
            "head": self._migration_head,
        }

    def _worker_check(self) -> dict[str, Any]:
        states: dict[str, bool] = {}
        for name, worker in (
            ("message", self.message_worker),
            ("reminder", self.reminder_worker),
            ("calendar", self.calendar_worker),
        ):
            if worker is None:
                continue
            try:
                states[name] = bool(worker.is_alive())
            except Exception:
                states[name] = False
        return {"ok": all(states.values()), "detail": states}

    def _queue_check(self) -> dict[str, Any]:
        if self.message_worker is None:
            return {"ok": True, "detail": "no worker"}
        try:
            depth = self.message_worker.queue_depth()
            backlog = self.message_worker.backlog()
            dead = self.message_worker.dead_letters()
        except Exception as exc:
            return {"ok": False, "detail": f"unreadable: {exc.__class__.__name__}"}
        # A backlog is a warning, not an outage, until it stops draining.
        return {
            "ok": backlog < self.backlog_threshold,
            "in_memory_depth": depth,
            "durable_backlog": backlog,
            "dead_lettered": dead,
        }

    def _whatsapp_check(self) -> dict[str, Any]:
        if self.transport is None:
            return {"ok": True, "detail": "no transport"}
        try:
            connected = bool(self.transport.is_connected())
        except Exception:
            return {"ok": False, "detail": "unknown"}
        return {"ok": connected, "detail": "connected" if connected else "disconnected"}


def build_handler(probes: HealthProbes) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "sounce-health"

        def do_GET(self) -> None:
            route = self.path.split("?", 1)[0].rstrip("/") or "/"
            if route in ("/", "/live", "/health"):
                self._respond(200, {"status": "live"})
                return
            if route == "/ready":
                ready, payload = probes.check()
                self._respond(200 if ready else 503, payload)
                return
            self._respond(404, {"status": "not_found"})

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    return _Handler


def start_health_server(port: int, probes: HealthProbes) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), build_handler(probes))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    log.info("Health server listening on :%s (/live, /ready)", port)
    return server
