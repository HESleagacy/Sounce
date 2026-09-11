"""Add recurrence, event categories, and structured document extraction."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reminders", sa.Column("recurrence_frequency", sa.String(20), nullable=True))
    op.add_column(
        "reminders",
        sa.Column("recurrence_interval", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("reminders", sa.Column("category", sa.String(100), nullable=True))
    op.add_column("timeline_events", sa.Column("category", sa.String(100), nullable=True))
    op.add_column("documents", sa.Column("document_type", sa.String(100), nullable=True))
    op.add_column("documents", sa.Column("extracted_dates", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("documents", sa.Column("extracted_amounts", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column(
        "documents", sa.Column("extracted_entities", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    op.drop_column("documents", "extracted_entities")
    op.drop_column("documents", "extracted_amounts")
    op.drop_column("documents", "extracted_dates")
    op.drop_column("documents", "document_type")
    op.drop_column("timeline_events", "category")
    op.drop_column("reminders", "category")
    op.drop_column("reminders", "recurrence_interval")
    op.drop_column("reminders", "recurrence_frequency")
