# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The OpenNVR updater (docs/design/auto-update-implementation-plan.md).

It ships inside the core image and runs as a one-off container
(``docker compose run --rm --no-deps opennvr-core python -m updater …``),
driven by the host-side verbs in start.sh / start.ps1. The installed
release's image checks a candidate before anything changes; the target
release's image verifies the stack after the switch.

Nothing here talks to Docker: the host does that. This package decides
(is this release genuine, may this site take it, did it come up right)
and the host acts.
"""

#: Bumped when the manifest gains a field an older updater would misread.
#: A manifest naming a higher ``min_updater`` is refused with a clear
#: message rather than half-understood.
UPDATER_PROTOCOL = 1
