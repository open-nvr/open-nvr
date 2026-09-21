# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""installed_apps.enabled — switch on the apps that are already running.

``enabled`` has been enforced as of this release: a disabled app gets no
camera roster, so it reads no frames and drives no adapters. Until now
it gated nothing an app could feel, and every app registered with it
FALSE — so on an upgraded deployment every running app would suddenly
stop, for a switch the operator was never asked about.

So the apps that registered under the old meaning are switched on here.
This is not "enable everything": a row is only touched if the app got as
far as registering, which means its container is deployed and was
working a moment ago. A licensed app that never cleared its entitlement
stays off (``entitlement_status``), because enabling one of those is a
licence decision, not a migration's. From now on an app is enabled on
its FIRST registration and the operator's Disable is honoured.

Revision ID: a1c8f4b27d93
Revises: c9d1e5f7a2b4
Create Date: 2026-09-20
"""
from alembic import op

revision = "a1c8f4b27d93"
down_revision = "c9d1e5f7a2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE installed_apps
           SET enabled = true
         WHERE enabled = false
           AND status = 'registered'
           AND (license_key_encrypted IS NULL
                OR entitlement_status = 'valid')
        """
    )


def downgrade() -> None:
    # One-way: which rows were false before this ran is not recorded, and
    # guessing would switch off apps the operator has since enabled.
    pass
