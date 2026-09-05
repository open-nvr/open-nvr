# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The app-registry contract — the compatibility promise, pinned.

docs/DEVELOPER_PROGRAM.md tells third-party developers that the shapes
the SDK reads from ``POST /apps/register``, ``GET /apps/{id}/config``
and ``GET /apps/{id}/status`` do not change under them without an
``API_VERSION`` bump, and that an SDK at ``MIN_SDK_VERSION`` keeps
working. This file is where that promise is enforced:

* every key the released SDK reads is present, with the type it expects;
* ``API_VERSION`` and ``MIN_SDK_VERSION`` are well-formed and the SDK in
  this repository satisfies its own server's minimum;
* the SDK's constants agree with the server's.

If a change here is deliberate, bump ``API_VERSION`` in
``routers/apps.py`` and say so in CHANGELOG.md — that is the whole
process.

Run with:
    cd server && pytest tests/test_registry_contract.py -v
"""
from __future__ import annotations

import datetime as _dt

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc  # noqa: UP017

import os
import re
import secrets
import sys
import types as _types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))
sys.path.insert(0, str(REPO_ROOT / "sdk" / "opennvr-app-sdk"))

from cryptography.fernet import Fernet  # noqa: E402

SITE_KEY = secrets.token_urlsafe(48)
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ["INTERNAL_API_KEY"] = SITE_KEY
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.auth as auth_mod  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from models import Role, User  # noqa: E402
from routers import apps as apps_router  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
API_VER = re.compile(r"^\d+\.\d+$")


def _vt(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _manifest(**over):
    m = {"id": "contract-probe", "name": "Contract Probe", "version": "1.0.0",
         "category": "perimeter", "summary": "", "requires_tasks": [],
         "subscribes": "opennvr.inference.>", "params": [], "emits": [],
         "provides": ["probe"]}
    m.update(over)
    return m


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "internal_api_key", SITE_KEY, raising=False)
    # The router resolves ``core.config.settings`` lazily; an earlier test
    # module may have swapped the module object in sys.modules, so patch
    # whichever one the router will see now, too.
    live_cfg = sys.modules.get("core.config")
    if live_cfg is not None and getattr(live_cfg, "settings", None) is not settings:
        monkeypatch.setattr(live_cfg.settings, "internal_api_key", SITE_KEY, raising=False)
    monkeypatch.setenv("INTERNAL_API_KEY", SITE_KEY)
    monkeypatch.setattr(auth_mod, "auth_logger", _L(), raising=False)
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    s = SessionLocal()
    role = Role(name="admin")
    s.add(role)
    s.flush()
    admin = User(username="admin", email="a@x", hashed_password="x",
                 role_id=role.id, is_superuser=True, is_active=True)
    s.add(admin)
    s.commit()
    s.close()

    app = FastAPI()
    app.include_router(apps_router.router)

    def _db():
        sess = SessionLocal()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_superuser] = lambda: admin
    with TestClient(app) as tc:
        yield tc


def _site():
    return {"X-Internal-Api-Key": SITE_KEY}


# ── versions ────────────────────────────────────────────────────────────


def test_version_constants_are_well_formed():
    assert API_VER.match(apps_router.API_VERSION), apps_router.API_VERSION
    assert SEMVER.match(apps_router.MIN_SDK_VERSION), apps_router.MIN_SDK_VERSION


def test_the_sdk_in_this_repo_satisfies_its_own_server():
    """A developer who pins the SDK shipped with a release must be able
    to register against that release."""
    from opennvr_app_sdk._version import __version__ as sdk_version

    assert SEMVER.match(sdk_version), sdk_version
    assert _vt(sdk_version) >= _vt(apps_router.MIN_SDK_VERSION), (
        f"SDK {sdk_version} is older than the server's MIN_SDK_VERSION "
        f"{apps_router.MIN_SDK_VERSION}; ship the SDK first")


def test_min_sdk_never_moves_past_what_the_examples_pin():
    """The example apps and the template are the reference SDK users;
    if they pin an SDK below the server minimum, CI would break for
    exactly the reason we promise it will not."""
    import tomllib

    pins = []
    for pyproject in [*REPO_ROOT.glob("examples/*/pyproject.toml"),
                      REPO_ROOT / "templates" / "opennvr-app" / "pyproject.toml"]:
        if not pyproject.exists():
            continue
        data = tomllib.loads(pyproject.read_text())
        for dep in data.get("project", {}).get("dependencies", []):
            m = re.match(r"opennvr[-_]app[-_]sdk\s*>=\s*(\d+\.\d+\.\d+)", dep)
            if m:
                pins.append((pyproject.parent.name, m.group(1)))
    for name, pin in pins:
        assert _vt(pin) >= _vt(apps_router.MIN_SDK_VERSION), (
            f"{name} pins opennvr-app-sdk>={pin}, below MIN_SDK_VERSION")


# ── response shapes the SDK reads ───────────────────────────────────────

#: key → accepted types. Removing a key or changing its type is a
#: MAJOR bump of API_VERSION. Adding keys is free (MINOR).
REGISTER_KEYS = {
    "id": (str,), "name": (str,), "category": (str,), "version": (str,),
    "url": (str,), "enabled": (bool,), "status": (str,),
    "last_seen": (str, type(None)), "manifest": (dict,), "config": (dict,),
    "has_api_key": (bool,), "api_key_issued_at": (str, type(None)),
    "entitlement": (dict,), "registry": (dict,),
}
REGISTRY_KEYS = {"server_version": (str,), "api_version": (str,),
                 "min_sdk_version": (str,)}
ENTITLEMENT_KEYS = {
    "mode": (str,), "status": (str,), "plan": (str, type(None)),
    "expires_at": (str, type(None)), "message": (str,), "limits": (dict,),
    "checked_at": (str, type(None)), "has_license_key": (bool,),
}
CONFIG_KEYS = {"id": (str,), "config": (dict,), "updated_at": (str, type(None)),
               "entitlement": (dict,)}
STATUS_KEYS = {"health": (dict,), "state": (dict, list, type(None))}


def _assert_shape(body: dict, spec: dict, where: str):
    for key, types in spec.items():
        assert key in body, f"{where}: missing '{key}'"
        assert isinstance(body[key], types), (
            f"{where}: '{key}' is {type(body[key]).__name__}, expected {types}")


def test_register_response_shape(env):
    r = env.post("/apps/register",
                 json={"url": "http://probe:9200", "manifest": _manifest(),
                       "sdk_version": apps_router.MIN_SDK_VERSION, "wants_key": True},
                 headers=_site())
    assert r.status_code == 200, r.text
    body = r.json()
    _assert_shape(body, REGISTER_KEYS, "register")
    _assert_shape(body["registry"], REGISTRY_KEYS, "register.registry")
    _assert_shape(body["entitlement"], ENTITLEMENT_KEYS, "register.entitlement")
    assert body["registry"]["api_version"] == apps_router.API_VERSION
    assert body["registry"]["min_sdk_version"] == apps_router.MIN_SDK_VERSION
    # The key is returned exactly once, in the clear, under this name.
    assert isinstance(body["api_key"], str) and body["api_key"].startswith("oak_")
    # A free app is enable-able out of the box.
    assert body["entitlement"]["mode"] == "none"
    assert body["entitlement"]["status"] == "none"


def test_old_sdk_is_warned_not_refused(env):
    """A too-old SDK still registers; the response says what it should
    upgrade to. Refusing would strand every deployed app on upgrade."""
    r = env.post("/apps/register",
                 json={"url": "http://probe:9200", "manifest": _manifest(),
                       "sdk_version": "0.0.1"},
                 headers=_site())
    assert r.status_code == 200, r.text
    assert r.json()["registry"]["min_sdk_version"] == apps_router.MIN_SDK_VERSION


def test_register_without_sdk_fields_still_works(env):
    """Pre-0.2 SDKs send only url + manifest."""
    r = env.post("/apps/register",
                 json={"url": "http://probe:9200", "manifest": _manifest()},
                 headers=_site())
    assert r.status_code == 200, r.text
    _assert_shape(r.json(), REGISTER_KEYS, "register(legacy)")


def test_config_and_status_response_shapes(env):
    key = env.post("/apps/register",
                   json={"url": "http://probe:9200", "manifest": _manifest()},
                   headers=_site()).json()["api_key"]
    app = {"X-Internal-Api-Key": key}

    cfg = env.get("/apps/contract-probe/config", headers=app)
    assert cfg.status_code == 200, cfg.text
    _assert_shape(cfg.json(), CONFIG_KEYS, "config")
    _assert_shape(cfg.json()["entitlement"], ENTITLEMENT_KEYS, "config.entitlement")

    st = env.get("/apps/contract-probe/status", headers=app)
    assert st.status_code == 200, st.text
    _assert_shape(st.json(), STATUS_KEYS, "status")
    assert "status" in st.json()["health"]     # "unreachable" here — still the shape


def test_licensed_app_contract(env):
    """The entitlement keys an app's ``verify_license`` verdict is
    mapped onto, and the 402 an operator sees before a valid key."""
    r = env.post("/apps/register",
                 json={"url": "http://probe:9200",
                       "manifest": _manifest(pricing="paid",
                                             entitlement="license_key")},
                 headers=_site())
    assert r.status_code == 200, r.text
    ent = r.json()["entitlement"]
    assert ent["mode"] == "license_key" and ent["has_license_key"] is False
    en = env.post("/apps/contract-probe/enable")
    assert en.status_code == 402, en.text


def test_sdk_constants_match_the_server():
    """The SDK ships the same field names it reads from the wire —
    catching a rename on either side before a release."""
    from opennvr_app_sdk import manifest as sdk_manifest
    from services import app_entitlements

    assert set(sdk_manifest.ENTITLEMENT_MODES) >= {"none", "license_key"}
    assert set(sdk_manifest.PRICING_MODELS) >= {"free", "paid"}
    # The server maps the app's verdict onto these — the SDK's
    # Entitlement dataclass carries the same names.
    from opennvr_app_sdk.contract import Entitlement

    for field in ("valid", "plan", "expires_at", "message", "limits"):
        assert field in Entitlement.__dataclass_fields__, field
    assert callable(app_entitlements.entitlement_view)
