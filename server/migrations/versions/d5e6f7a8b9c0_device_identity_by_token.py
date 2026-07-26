"""device firewall: identify browsers by a server-issued token, not by IP

Revision ID: d5e6f7a8b9c0
Revises: c3d4e5f6a7b8
Create Date: 2026-07-25

An IP address cannot identify a device: NAT collapses every client behind one
address (with Docker Desktop's port forwarding, all LAN clients arrive as the
bridge gateway), so per-device approval was unenforceable — a second device
silently inherited the first one's approval. Identity moves to the SHA-256 of a
random token the server issues at login and stores in an HttpOnly cookie.

Existing rows are keyed by an address we no longer trust, so they are removed:
the next login re-bootstraps trust-on-first-use (first browser auto-approved).
Recovery paths are unaffected — loopback is always allowed, ``python -m
manage_devices`` still works, and ``DEVICE_FIREWALL_KILL=true`` forces
enforcement off.

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trusted_devices",
        sa.Column("device_token_hash", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_trusted_devices_device_token_hash",
        "trusted_devices",
        ["device_token_hash"],
        unique=True,
    )
    # ip_address becomes metadata: nullable, and no longer unique (many browsers
    # legitimately share one address behind NAT).
    op.drop_index("ix_trusted_devices_ip_address", table_name="trusted_devices")
    op.alter_column(
        "trusted_devices",
        "ip_address",
        existing_type=sa.String(length=45),
        nullable=True,
    )
    op.create_index(
        "ix_trusted_devices_ip_address",
        "trusted_devices",
        ["ip_address"],
        unique=False,
    )
    # Drop IP-identified rows — they cannot be matched to a browser token, and
    # keeping an "approved" one would both grant nothing and block the
    # trust-on-first-use bootstrap.
    op.execute("DELETE FROM trusted_devices")


def downgrade() -> None:
    # Tokens have no IP equivalent; rows are dropped again so the restored
    # unique-IP constraint can never collide.
    op.execute("DELETE FROM trusted_devices")
    op.drop_index("ix_trusted_devices_ip_address", table_name="trusted_devices")
    op.alter_column(
        "trusted_devices",
        "ip_address",
        existing_type=sa.String(length=45),
        nullable=False,
    )
    op.create_index(
        "ix_trusted_devices_ip_address",
        "trusted_devices",
        ["ip_address"],
        unique=True,
    )
    op.drop_index(
        "ix_trusted_devices_device_token_hash", table_name="trusted_devices"
    )
    op.drop_column("trusted_devices", "device_token_hash")
