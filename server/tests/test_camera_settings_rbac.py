# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Camera-device settings access model: GET routes are readable by the camera's
owner; EVERY mutating route additionally requires ``camera_device.write``
(held implicitly by superusers). The structural test walks the real router so
a future write endpoint added without the gate fails CI."""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import HTTPException  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from routers.camera_settings import require_device_write, router  # noqa: E402


class _FakeUser:
    def __init__(self, superuser: bool, perms: set[str] | None = None):
        self.is_superuser = superuser
        self.role = (
            _types.SimpleNamespace(
                permissions=[_types.SimpleNamespace(name=p) for p in (perms or set())]
            )
            if perms is not None
            else None
        )


# --- the dependency itself ---


def test_superuser_passes_write_gate():
    user = _FakeUser(superuser=True)
    assert require_device_write(current_user=user) is user


def test_plain_user_is_denied_writes():
    with pytest.raises(HTTPException) as exc:
        require_device_write(current_user=_FakeUser(superuser=False))
    assert exc.value.status_code == 403


def test_role_with_named_permission_passes():
    user = _FakeUser(superuser=False, perms={"camera_device.write"})
    assert require_device_write(current_user=user) is user


def test_role_with_full_access_wildcard_passes():
    user = _FakeUser(superuser=False, perms={"full_access"})
    assert require_device_write(current_user=user) is user


# --- structural guarantee over the real router ---


def _has_write_gate(route: APIRoute) -> bool:
    return any(
        dep.call is require_device_write for dep in route.dependant.dependencies
    )


def test_every_mutating_route_is_write_gated():
    """Any POST/PUT/DELETE/PATCH under /cameras must carry the write gate —
    including the dangerous four (camera-user create/delete, reboot, config
    export). Adding a write endpoint without the gate fails here."""
    ungated = [
        f"{sorted(r.methods)} {r.path}"
        for r in router.routes
        if isinstance(r, APIRoute)
        and (r.methods - {"GET", "HEAD"})
        and not _has_write_gate(r)
    ]
    assert not ungated, f"write routes missing require_device_write: {ungated}"


def test_read_routes_stay_open_to_owners():
    """GET routes must NOT demand the write permission — device settings are
    explicitly read-only (not invisible) for non-admin users."""
    gated_reads = [
        r.path
        for r in router.routes
        if isinstance(r, APIRoute) and r.methods == {"GET"} and _has_write_gate(r)
    ]
    assert not gated_reads, f"read routes wrongly write-gated: {gated_reads}"


def test_dangerous_four_are_covered():
    """Belt-and-braces: the endpoints from the review finding, by name."""
    by_name = {
        r.name: r for r in router.routes if isinstance(r, APIRoute)
    }
    for name in (
        "create_camera_user",
        "delete_camera_user",
        "reboot_camera",
        "export_camera_config",
    ):
        assert name in by_name, f"route {name} missing"
        assert _has_write_gate(by_name[name]), f"{name} lost its write gate"
