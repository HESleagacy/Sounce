from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.assistant.decision_engine import DecisionEngine
from app.assistant.service import AssistantService
from app.config import Settings, get_settings
from app.health import HealthProbes, start_health_server
from app.persistence.database import Database
from app.persistence.repositories import Repository
from app.privacy import purge_user_data, write_export
from app.providers.gemini import GeminiProvider
from app.providers.google_calendar import build_calendar_provider
from app.providers.maya import build_tts_provider
from app.providers.web import SafeWebFetcher
from app.transport.neonize_adapter import NeonizeAdapter
from app.workers.calendar_worker import CalendarWorker
from app.workers.message_worker import MessageWorker
from app.workers.reminder_worker import ReminderWorker
from app.workers.retention_worker import RetentionWorker

log = logging.getLogger(__name__)

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


def run_migrations(database_url: str) -> None:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def connect_with_retry(
    transport: NeonizeAdapter,
    initial_seconds: int,
    max_seconds: int,
) -> None:
    delay = initial_seconds
    while True:
        try:
            transport.connect()
            log.warning("WhatsApp connection ended; reconnecting in %s seconds", delay)
        except Exception:
            log.exception("WhatsApp connection failed; retrying in %s seconds", delay)
        time.sleep(delay)
        delay = min(delay * 2, max_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sounce",
        description="Sounce — a private multilingual WhatsApp assistant",
    )
    parser.add_argument(
        "--self-chat-check",
        action="store_true",
        help="Log normalized owner self-chat events without invoking Gemini or replying",
    )
    subcommands = parser.add_subparsers(dest="command")

    export = subcommands.add_parser("export", help="Write everything stored about the owner to a JSON file")
    export.add_argument("--out", type=Path, default=None, help="Destination file")

    purge = subcommands.add_parser("purge", help="Erase all stored data for the owner")
    purge.add_argument("--yes", action="store_true", help="Required. Confirms the erase is intentional.")

    retention = subcommands.add_parser(
        "retention", help="Apply the retention policy to raw messages and media now"
    )
    retention.add_argument(
        "--days",
        type=int,
        default=None,
        help="Override RETENTION_DAYS for this run",
    )

    subcommands.add_parser("health", help="Print the readiness report and exit")
    return parser


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _owner_user_id(database: Database, settings: Settings) -> int | None:
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(settings.owner_jid, settings.owner_timezone)
        return user.id


def command_export(settings: Settings, destination: Path | None) -> int:
    database = Database(settings.database_url)
    user_id = _owner_user_id(database, settings)
    if user_id is None:
        print("No stored data for the configured owner.", file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination or settings.export_dir / f"sounce-export-{stamp}.json"
    with database.session() as session:
        path = write_export(session, user_id, target)
    print(f"Exported to {path}")
    return 0


def command_purge(settings: Settings, confirmed: bool) -> int:
    if not confirmed:
        print(
            "Refusing to erase without --yes. This permanently deletes every stored\n"
            "message, transcript, memory, reminder, document and media file.",
            file=sys.stderr,
        )
        return 2
    database = Database(settings.database_url)
    user_id = _owner_user_id(database, settings)
    if user_id is None:
        print("Nothing to erase.")
        return 0
    with database.session() as session:
        counts = purge_user_data(session, user_id, settings.media_dir)
    print(json.dumps(counts, indent=2))
    return 0


def command_retention(settings: Settings, days: int | None) -> int:
    database = Database(settings.database_url)
    retention_days = settings.retention_days if days is None else days
    worker = RetentionWorker(database, retention_days, settings.media_dir)
    if not worker.enabled:
        print("Retention is disabled (0 days); nothing removed.")
        return 0
    counts = worker.run_once()
    print(json.dumps(counts, indent=2))
    return 0


def command_health(settings: Settings) -> int:
    database = Database(settings.database_url)
    probes = HealthProbes(
        database=database,
        alembic_ini=ALEMBIC_INI,
        provider_configured=bool(settings.gemini_api_key.get_secret_value()),
        backlog_threshold=settings.health_backlog_threshold,
    )
    ready, payload = probes.check()
    print(json.dumps(payload, indent=2))
    return 0 if ready else 1


def run_self_chat_check(settings: Settings) -> None:
    transport = NeonizeAdapter(settings.neonize_session_path, settings.owner_jid, diagnostics=True)
    transport.set_message_handler(
        lambda message: log.info(
            "Self-chat check: id=%s from_me=%s type=%s text=%r",
            message.whatsapp_message_id,
            message.is_from_me,
            message.message_type,
            message.text,
        )
    )
    log.info("Self-chat check enabled; Gemini and database processing are disabled")
    connect_with_retry(
        transport,
        settings.whatsapp_reconnect_initial_seconds,
        settings.whatsapp_reconnect_max_seconds,
    )


def run_assistant(settings: Settings) -> None:
    run_migrations(settings.database_url)
    database = Database(settings.database_url)
    gemini_api_key = settings.gemini_api_key.get_secret_value()
    if not gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY is required outside --self-chat-check mode")

    transport = NeonizeAdapter(settings.neonize_session_path, settings.owner_jid)
    provider = GeminiProvider(gemini_api_key, settings.gemini_model)
    calendar = build_calendar_provider(
        settings.google_client_id,
        settings.google_client_secret.get_secret_value(),
        settings.google_refresh_token.get_secret_value(),
        settings.google_calendar_id,
        settings.google_token_path,
    )
    if calendar is not None:
        log.info("Google Calendar synchronization enabled (via the durable outbox)")
    tts = build_tts_provider(
        settings.maya_api_url,
        settings.maya_api_key.get_secret_value(),
        settings.maya_voice,
        settings.maya_model,
    )
    if tts is not None:
        log.info("Maya text-to-speech enabled with text fallback")

    assistant = AssistantService(
        provider,
        DecisionEngine(settings.pending_action_ttl_minutes, calendar),
    )
    message_worker = MessageWorker(
        database=database,
        assistant=assistant,
        transport=transport,
        owner_jid=settings.owner_jid,
        owner_timezone=settings.owner_timezone,
        media_dir=settings.media_dir,
        max_media_bytes=settings.max_media_bytes,
        tts=tts,
        web_fetcher=SafeWebFetcher(settings.max_media_bytes),
        queue_size=settings.message_queue_size,
        lease_seconds=settings.message_lease_seconds,
        max_attempts=settings.message_max_attempts,
        backoff_seconds=settings.message_backoff_seconds,
    )
    reminder_worker = ReminderWorker(
        database=database,
        transport=transport,
        owner_jid=settings.owner_jid,
        poll_seconds=settings.reminder_poll_seconds,
        lease_seconds=settings.reminder_lease_seconds,
        max_attempts=settings.reminder_max_attempts,
    )
    calendar_worker = (
        CalendarWorker(
            database=database,
            calendar=calendar,
            poll_seconds=settings.calendar_poll_seconds,
            max_attempts=settings.calendar_max_attempts,
            backoff_seconds=settings.calendar_backoff_seconds,
        )
        if calendar is not None
        else None
    )
    retention_worker = RetentionWorker(
        database=database,
        retention_days=settings.retention_days,
        media_dir=settings.media_dir,
        interval_hours=settings.retention_interval_hours,
    )

    port = os.environ.get("PORT")
    if port:
        start_health_server(
            int(port),
            HealthProbes(
                database=database,
                alembic_ini=ALEMBIC_INI,
                message_worker=message_worker,
                reminder_worker=reminder_worker,
                calendar_worker=calendar_worker,
                transport=transport,
                provider_configured=True,
                backlog_threshold=settings.health_backlog_threshold,
            ),
        )

    transport.set_message_handler(message_worker.enqueue)
    message_worker.start()
    reminder_worker.start()
    if calendar_worker is not None:
        calendar_worker.start()
    retention_worker.start()
    try:
        connect_with_retry(
            transport,
            settings.whatsapp_reconnect_initial_seconds,
            settings.whatsapp_reconnect_max_seconds,
        )
    finally:
        retention_worker.stop()
        if calendar_worker is not None:
            calendar_worker.stop()
        reminder_worker.stop()
        message_worker.stop()
        database.dispose()


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    _configure_logging(settings)

    if args.command == "export":
        raise SystemExit(command_export(settings, args.out))
    if args.command == "purge":
        raise SystemExit(command_purge(settings, args.yes))
    if args.command == "retention":
        raise SystemExit(command_retention(settings, args.days))
    if args.command == "health":
        raise SystemExit(command_health(settings))

    if args.self_chat_check:
        run_self_chat_check(settings)
        return
    run_assistant(settings)


if __name__ == "__main__":
    main()
