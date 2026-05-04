"""smtp_settings: singleton SMTP config table

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "smtp_settings",
        sa.Column("id",          sa.Integer(),     primary_key=True),
        sa.Column("host",        sa.String(200),   server_default="", nullable=False),
        sa.Column("port",        sa.Integer(),     server_default="587", nullable=False),
        sa.Column("username",    sa.String(200),   server_default="", nullable=False),
        sa.Column("password",    sa.String(500),   server_default="", nullable=False),
        sa.Column("from_email",  sa.String(200),   server_default="", nullable=False),
        sa.Column("from_name",   sa.String(100),   server_default="Kalipeh", nullable=False),
        sa.Column("use_tls",     sa.Boolean(),     server_default="true",  nullable=False),
        sa.Column("use_ssl",     sa.Boolean(),     server_default="false", nullable=False),
        sa.Column("enabled",     sa.Boolean(),     server_default="false", nullable=False),
        sa.Column("updated_at",  sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # Insert the singleton row
    op.execute("INSERT INTO smtp_settings (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("smtp_settings")
