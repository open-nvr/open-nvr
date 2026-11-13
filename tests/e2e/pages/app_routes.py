# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every routed page of the SPA, named once.

Shared by the route smoke test and the UI sweep so the two can never cover
different pages. Mirrors ``app/src/main.tsx`` plus the sub-tabs of Settings,
Access Control and Network; add a route here when you add one there.

Dynamic routes (``/apps/:appId``, ``/app-catalog/:appId``) need an installed
app and are left to the app tests.
"""

from __future__ import annotations

ROUTES: list[str] = [
    "/",
    "/live",
    "/playback/sync",
    "/search",
    "/cameras",
    "/alarms",
    "/alerts-incidents",
    # Applications: on a bare stack each renders its "not installed" state,
    # which is exactly the path worth proving does not throw.
    "/vehicles",
    "/occupancy",
    "/people",
    "/tripwires",
    "/loitering",
    "/perimeter",
    "/left-items",
    "/deliveries",
    "/gates",
    "/notifications",
    "/guard-compliance",
    "/app-catalog",
    # AI & detections
    "/byom",
    "/ai-detection-results",
    "/ai-adapters",
    # Security & network
    "/network/camera-lan",
    "/network/uplink",
    "/logs",
    # Governance
    "/audit-logs",
    "/compliance",
    "/rbac/users",
    "/rbac/roles",
    "/rbac/permissions",
    "/rbac/password-policy",
    "/byok",
    # Administration
    "/updates",
    "/integrations",
    "/cloud",
    "/firmware",
    "/support",
    # Settings sub-pages
    "/settings",
    "/settings/camera-config/device",
    "/settings/camera-config/streaming",
    "/settings/camera-config/zones",
    "/settings/recording",
    "/settings/deleted-cameras",
    "/settings/media-source/settings",
    "/settings/media-source/media-server-manager",
    "/settings/firewall",
    "/settings/api-tokens",
    "/settings/more-settings/webrtc",
    "/settings/more-settings/window-settings",
    "/settings/more-settings/camera-tours",
    "/settings/more-settings/uplink",
    "/settings/more-settings/system-health",
]

__all__ = ["ROUTES"]
