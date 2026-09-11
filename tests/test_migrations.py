"""Migrations must produce exactly the schema the models declare."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.main import run_migrations
from app.persistence.models import Base
from sqlalchemy import create_engine, inspect

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


def _config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_migrations_produce_the_declared_schema(tmp_path: Path) -> None:
    """The drift guard.

    Adding a column to a model without writing the migration is the easiest way
    to ship a release that crashes on someone else's database. This fails first.
    """
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    run_migrations(url)
    inspector = inspect(create_engine(url))

    migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
    declared_tables = set(Base.metadata.tables)
    assert migrated_tables == declared_tables

    for table in sorted(declared_tables):
        migrated_columns = {column["name"] for column in inspector.get_columns(table)}
        declared_columns = {column.name for column in Base.metadata.tables[table].columns}
        assert migrated_columns == declared_columns, f"column drift in {table!r}"


def test_the_unique_guard_on_reminder_occurrences_exists(tmp_path: Path) -> None:
    """This constraint is what makes delivery idempotent; it must survive migration."""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    run_migrations(url)
    inspector = inspect(create_engine(url))

    constraints = inspector.get_unique_constraints("reminder_deliveries")
    assert any(
        set(constraint["column_names"]) == {"reminder_id", "occurrence_key"} for constraint in constraints
    )


def test_every_migration_has_a_single_head(tmp_path: Path) -> None:
    script = ScriptDirectory.from_config(_config(f"sqlite:///{tmp_path / 'x.db'}"))
    assert len(script.get_heads()) == 1


def test_migrations_are_reversible(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "0004")

    inspector = inspect(create_engine(url))
    tables = set(inspector.get_table_names())
    assert "inbound_jobs" not in tables
    assert "reminder_deliveries" not in tables
    assert "calendar_ops" not in tables

    command.upgrade(config, "head")
    inspector = inspect(create_engine(url))
    assert "inbound_jobs" in set(inspector.get_table_names())
