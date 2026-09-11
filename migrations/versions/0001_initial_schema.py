"""Create the initial Sounce schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("whatsapp_jid", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("whatsapp_jid"),
    )
    op.create_index("ix_users_whatsapp_jid", "users", ["whatsapp_jid"], unique=True)
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("whatsapp_message_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(30), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("transcript", sa.Text()),
        sa.Column("media_path", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("whatsapp_message_id"),
    )
    op.create_index("ix_messages_whatsapp_message_id", "messages", ["whatsapp_message_id"], unique=True)
    op.create_index("ix_messages_user_id", "messages", ["user_id"])
    _create_source_linked_tables()


def _create_source_linked_tables() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memories_user_id", "memories", ["user_id"])
    op.create_table(
        "preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "key", name="uq_preferences_user_key"),
    )
    op.create_index("ix_preferences_user_id", "preferences", ["user_id"])
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_pending_actions_user_id", "pending_actions", ["user_id"])
    op.create_index("ix_pending_actions_status", "pending_actions", ["status"])
    op.create_index("ix_pending_actions_expires_at", "pending_actions", ["expires_at"])
    _create_future_domain_tables()


def _create_future_domain_tables() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reminders_user_id", "reminders", ["user_id"])
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])
    op.create_index("ix_reminders_status", "reminders", ["status"])
    op.create_table(
        "timeline_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
    )
    op.create_index("ix_timeline_events_user_id", "timeline_events", ["user_id"])
    op.create_index("ix_timeline_events_starts_at", "timeline_events", ["starts_at"])
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_documents_user_id", "documents", ["user_id"])


def downgrade() -> None:
    for table in (
        "documents",
        "timeline_events",
        "reminders",
        "pending_actions",
        "preferences",
        "memories",
        "messages",
        "users",
    ):
        op.drop_table(table)
