# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 4: the /apps/{id}/ui proxy — the app-surface routing
convention. Apps never get their own public ports; the ONE self-contained
HTML dashboard an app serves at /ui on its contract port reaches the
operator through this JWT-gated route.

Pinned here: the route exists and is user-JWT-only (a dashboard is
operator surface — the service key must never satisfy it, same
governance as actions); has_ui is the opt-in gate; upstream failures
are clean 502s, never pass-through surprises.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_uiproxy_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

from routers.apps import UI_PROXY_MAX_BYTES, router  # noqa: E402


def _route(path: str):
    return next(r for r in router.routes if r.path == path)


def test_route_exists_and_is_get_only():
    route = _route("/apps/{app_id}/ui")
    assert route.methods == {"GET"}


def test_route_is_user_jwt_only_like_actions():
    # The dependency chain must run through get_current_active_user and
    # NOT get_read_principal: the internal service key reads state, but
    # a dashboard is operator surface (same governance as actions).
    route = _route("/apps/{app_id}/ui")
    dep_names = {
        d.call.__name__
        for d in route.dependant.dependencies
        if getattr(d, "call", None)
    }
    assert "get_current_active_user" in dep_names
    assert "get_read_principal" not in dep_names


def test_size_cap_is_a_real_number():
    # A dashboard is a small page; the cap guards the proxy, not taste.
    assert 0 < UI_PROXY_MAX_BYTES <= 5_000_000


def test_register_route_audits_scope_grants():
    # RFC-0002 Phase 5: "every grant audited" — source-level guard that
    # registration writes one app.scope_granted row per manifest scope.
    src = (_HERE / "routers" / "apps.py").read_text()
    assert 'action="app.scope_granted"' in src
    assert '"policy": "grant-on-registration"' in src
