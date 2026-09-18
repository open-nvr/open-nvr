# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Version of the API contract that external integrations depend on.

The Home Assistant integration (and anything else built on pyopennvr) reads
this from GET /api/v1/system/info and refuses to run against a server whose
contract it does not support, with a Repair telling the user what to upgrade
(design §6.11).

Rules: additive changes (new fields, new messages) bump the MINOR version;
removing or renaming anything bumps the MAJOR version and needs a two-release
deprecation window. Frozen at 1.0.0 by HA-115: the machine-readable contract
is ``server/contract/contract.json`` (endpoints, websocket v2 frames,
descriptor fields, payload schemas), with example payloads in
``server/contract/fixtures/``. ``scripts/contract_check.py`` fails CI when it
changes without the matching bump; the tests hold the code to it.
"""

CONTRACT_VERSION = "1.0.0"

#: Capabilities an integration may feature-detect. Each issue that adds a
#: client-visible capability appends its name here.
FEATURES: tuple[str, ...] = (
    "correlation_id",       # HA-002: X-Correlation-Id honoured and audited
    "ws_device_firewall",   # HA-006: the events WebSocket is firewalled
    "system_info",          # HA-104: this endpoint
    "camera_stats",         # HA-105: GET /cameras/{id}/stats
    "api_tokens",           # HA-101: scoped, revocable API tokens
    "ws_token_tickets",     # HA-103: tokens open the events socket
    "detection_toggle",     # HA-106: per-camera detection on/off
    "ptz_presets",          # HA-107
    "manual_events",        # HA-107: POST /events, end, protect
    "recording_pause",      # HA-108: behind the site flag (see recording_pause_enabled)
    "zones",                # HA-109
    "live_state",           # HA-110
    "ws_v2",                # HA-111: seq, resume, snapshot, types
    "signed_media",         # HA-112
    "media_ready",          # HA-113
    "site_mode",            # HA-118
    "entities",             # HA-114: server-described entities
    "search",               # HA-116
)
