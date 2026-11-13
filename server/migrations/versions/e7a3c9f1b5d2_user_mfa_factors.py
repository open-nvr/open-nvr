# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""user_mfa_factors: an account may hold several authenticator apps.

Each enrolled account's existing TOTP secret becomes its first factor,
labelled "Authenticator". The ciphertext is copied as it is (same key), so
nothing is decrypted here. A half-finished enrolment (a secret on a row with
mfa_enabled false) is not carried over; sign-in already treats such an
account as not enrolled.

users.encrypted_mfa_secret stays, kept equal to the oldest enabled factor by
the application, so an older core started on this database still verifies
the right code. Downgrade writes that factor back for the same reason.

create_all runs before Alembic at startup, so on an upgraded install the
table usually exists already, empty: the table is created only when missing,
and the copy skips users who already have a factor.

Revision ID: e7a3c9f1b5d2
Revises: d5e1f7a9c3b4
Create Date: 2026-11-13
"""
import sqlalchemy as sa
from alembic import op

revision = "e7a3c9f1b5d2"
down_revision = "d5e1f7a9c3b4"
branch_labels = None
depends_on = None

TABLE = "user_mfa_factors"


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="totp"),
            sa.Column("label", sa.String(length=64), nullable=False, server_default="Authenticator"),
            sa.Column("encrypted_secret", sa.String(length=500), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        )
    existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(TABLE)}
    for col in ("id", "user_id"):
        name = f"ix_{TABLE}_{col}"
        if name not in existing:
            op.create_index(name, TABLE, [col])

    op.execute(sa.text(f"""
        INSERT INTO {TABLE} (user_id, kind, label, encrypted_secret, enabled, confirmed_at, created_at)
        SELECT u.id, 'totp', 'Authenticator', u.encrypted_mfa_secret, true,
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM users u
        WHERE u.encrypted_mfa_secret IS NOT NULL
          AND u.mfa_enabled = true
          AND NOT EXISTS (SELECT 1 FROM {TABLE} f WHERE f.user_id = u.id)
    """))


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        return
    # Each user's oldest enabled, confirmed factor back into the old column;
    # an account with none is left without a secret (and not enrolled).
    op.execute(sa.text(f"""
        UPDATE users SET encrypted_mfa_secret = (
            SELECT f.encrypted_secret FROM {TABLE} f
            WHERE f.user_id = users.id AND f.enabled = true AND f.confirmed_at IS NOT NULL
            ORDER BY f.id LIMIT 1
        )
    """))
    op.execute(sa.text(f"""
        UPDATE users SET mfa_enabled = false
        WHERE encrypted_mfa_secret IS NULL AND (auth_provider IS NULL OR auth_provider <> 'oidc')
    """))
    op.drop_table(TABLE)
