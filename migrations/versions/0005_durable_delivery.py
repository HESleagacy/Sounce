"""Durable inbox, idempotent reminder delivery, and a Calendar outbox."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("whatsapp_message_id", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_inbound_jobs_whatsapp_message_id", "inbound_jobs", ["whatsapp_message_id"], unique=True
    )
    op.create_index("ix_inbound_jobs_status", "inbound_jobs", ["status"])
    op.create_index("ix_inbound_jobs_available_at", "inbound_jobs", ["available_at"])
    op.create_index("ix_inbound_jobs_status_available", "inbound_jobs", ["status", "available_at"])

    op.create_table(
        "reminder_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reminder_id", sa.Integer(), sa.ForeignKey("reminders.id"), nullable=False),
        sa.Column("occurrence_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="sending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("reminder_id", "occurrence_key", name="uq_reminder_deliveries_occurrence"),
    )
    op.create_index("ix_reminder_deliveries_reminder_id", "reminder_deliveries", ["reminder_id"])
    op.create_index("ix_reminder_deliveries_status", "reminder_deliveries", ["status"])

    op.create_table(
        "calendar_ops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("operation_id", sa.String(64), nullable=False),
        sa.Column("op", sa.String(20), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_calendar_ops_operation_id", "calendar_ops", ["operation_id"], unique=True)
    op.create_index("ix_calendar_ops_user_id", "calendar_ops", ["user_id"])
    op.create_index("ix_calendar_ops_status", "calendar_ops", ["status"])
    op.create_index("ix_calendar_ops_available_at", "calendar_ops", ["available_at"])
    op.create_index("ix_calendar_ops_status_available", "calendar_ops", ["status", "available_at"])

    op.add_column(
        "reminders",
        sa.Column("calendar_sync_status", sa.String(20), nullable=False, server_default="not_required"),
    )
    op.add_column("reminders", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "reminders", sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("reminders", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_index("ix_reminders_lease_expires_at", "reminders", ["lease_expires_at"])
    op.add_column(
        "timeline_events",
        sa.Column("calendar_sync_status", sa.String(20), nullable=False, server_default="not_required"),
    )

    # Rows written before this migration already have a remote event, so record
    # that fact rather than leaving them looking unsynced.
    op.execute("UPDATE reminders SET calendar_sync_status = 'synced' WHERE calendar_event_id IS NOT NULL")
    op.execute(
        "UPDATE timeline_events SET calendar_sync_status = 'synced' WHERE calendar_event_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("timeline_events", "calendar_sync_status")
    op.drop_index("ix_reminders_lease_expires_at", table_name="reminders")
    op.drop_column("reminders", "last_error")
    op.drop_column("reminders", "delivery_attempts")
    op.drop_column("reminders", "lease_expires_at")
    op.drop_column("reminders", "calendar_sync_status")
    op.drop_table("calendar_ops")
    op.drop_table("reminder_deliveries")
    op.drop_table("inbound_jobs")
