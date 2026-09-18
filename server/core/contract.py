# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Version of the API contract that external integrations depend on.

The Home Assistant integration (and anything else built on pyopennvr) reads
this from GET /api/v1/system/info and refuses to run against a server whose
contract it does not support, with a Repair telling the user what to upgrade
(design §6.11).

Rules: additive changes (new fields, new messages) bump the MINOR version;
removing or renaming anything bumps the MAJOR version and needs a two-release
deprecation window. It stays 0.x while M1 builds the contract, and is frozen
at 1.0.0 by HA-115.
"""

CONTRACT_VERSION = "0.1.0"

#: Capabilities an integration may feature-detect. Each issue that adds a
#: client-visible capability appends its name here.
FEATURES: tuple[str, ...] = (
    "correlation_id",       # HA-002: X-Correlation-Id honoured and audited
    "ws_device_firewall",   # HA-006: the events WebSocket is firewalled
    "system_info",          # HA-104: this endpoint
)
