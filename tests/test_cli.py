"""The data-lifecycle CLI and the retention worker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app import main as cli
from app.config import Settings
from app.domain.messages import InboundMessage, MessageType
from app.main import build_parser, command_export, command_health, command_purge, command_retention
from app.persistence.database import Database
from app.persistence.models import Message, User
from app.persistence.repositories import Repository
from app.workers.retention_worker import RetentionWorker
from sqlalchemy import select

OWNER = "919876543210@s.whatsapp.net"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "owner_jid": OWNER,
        "owner_timezone": "Asia/Kolkata",
        "database_url": f"sqlite:///{tmp_path / 'cli.db'}",
        "media_dir": tmp_path / "media",
        "export_dir": tmp_path / "exports",
        "retention_days": 365,
    }
    values.update(overrides)
    return Settings(**values)


def seed(settings: Settings, age_days: int = 500) -> Database:
    cli.run_migrations(settings.database_url)
    database = Database(settings.database_url)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    with database.session() as session:
        repository = Repository(session)
        user = repository.get_or_create_user(settings.owner_jid, settings.owner_timezone)
        message = repository.add_inbound(
            user,
            InboundMessage(
                whatsapp_message_id="old",
                chat_jid=OWNER,
                sender_jid=OWNER,
                message_type=MessageType.TEXT,
                text="purana message",
                occurred_at=datetime.now(timezone.utc) - timedelta(days=age_days),
                is_from_me=True,
                is_self_chat=True,
            ),
        )
        message.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    return database


def test_parser_exposes_the_lifecycle_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["export", "--out", "x.json"]).command == "export"
    assert parser.parse_args(["purge", "--yes"]).yes is True
    assert parser.parse_args(["retention", "--days", "30"]).days == 30
    assert parser.parse_args(["health"]).command == "health"
    # No subcommand still means "run the assistant".
    assert parser.parse_args([]).command is None
    assert parser.parse_args(["--self-chat-check"]).self_chat_check is True


def test_export_writes_a_readable_file(tmp_path: Path, capsys) -> None:
    settings = make_settings(tmp_path)
    seed(settings)
    target = tmp_path / "out.json"

    assert command_export(settings, target) == 0

    payload = json.loads(target.read_text())
    assert payload["user"]["whatsapp_jid"] == OWNER
    assert payload["messages"][0]["text"] == "purana message"
    assert str(target) in capsys.readouterr().out


def test_export_defaults_into_the_export_directory(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings)

    assert command_export(settings, None) == 0

    written = list(settings.export_dir.glob("sounce-export-*.json"))
    assert len(written) == 1


def test_purge_refuses_without_explicit_confirmation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = seed(settings)

    assert command_purge(settings, confirmed=False) == 2

    with database.session() as session:
        assert session.scalar(select(Message)) is not None


def test_purge_erases_when_confirmed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = seed(settings)

    assert command_purge(settings, confirmed=True) == 0

    with database.session() as session:
        assert session.scalar(select(Message)) is None
        assert session.scalar(select(User)) is None


def test_retention_command_reports_what_it_removed(tmp_path: Path, capsys) -> None:
    settings = make_settings(tmp_path)
    database = seed(settings)

    assert command_retention(settings, days=None) == 0

    counts = json.loads(capsys.readouterr().out)
    assert counts["messages"] == 1
    with database.session() as session:
        assert session.scalar(select(Message)) is None


def test_retention_command_honours_a_disabled_policy(tmp_path: Path, capsys) -> None:
    settings = make_settings(tmp_path, retention_days=0)
    database = seed(settings)

    assert command_retention(settings, days=None) == 0

    assert "disabled" in capsys.readouterr().out
    with database.session() as session:
        assert session.scalar(select(Message)) is not None


def test_health_command_exits_nonzero_when_not_ready(tmp_path: Path, capsys) -> None:
    settings = make_settings(tmp_path, gemini_api_key="")
    seed(settings)

    assert command_health(settings) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["decision_provider"]["ok"] is False


def test_retention_worker_is_a_noop_when_disabled(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, retention_days=0)
    database = seed(settings)
    worker = RetentionWorker(database, 0, settings.media_dir)

    assert worker.enabled is False
    assert worker.is_alive() is True  # a disabled worker is not a failed worker
    worker.start()
    worker.stop()
    with database.session() as session:
        assert session.scalar(select(Message)) is not None


def test_retention_worker_sweeps_every_user(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = seed(settings)
    worker = RetentionWorker(database, 365, settings.media_dir)

    counts = worker.run_once()

    assert counts["messages"] == 1
    with database.session() as session:
        assert session.scalar(select(Message)) is None


@pytest.mark.parametrize("command", ["export", "purge", "retention", "health"])
def test_main_dispatches_each_command(tmp_path: Path, monkeypatch, command: str) -> None:
    settings = make_settings(tmp_path)
    seed(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    argv = ["sounce", command] + (["--yes"] if command == "purge" else [])
    monkeypatch.setattr("sys.argv", argv)

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code in (0, 1)
