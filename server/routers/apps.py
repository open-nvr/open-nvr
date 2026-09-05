# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
App Registry Router — the missing seam from App SDK spec §05.

Apps built on the OpenNVR App SDK self-register on boot
(``POST /apps/register``), exactly the shape adapters already use
against KAI-C. The registry stores the ``AppManifest.to_dict()``
snapshot plus operator config, and because ``manifest.params`` is
typed and declarative, ``PUT /apps/{id}/config`` validates without any
app-specific code (see :func:`validate_app_config`).

Routes (mounted under ``/api/v1``):

- ``GET  /apps``                 — catalog listing
- ``POST /apps/register``        — app calls this on boot (upsert)
- ``POST /apps/{id}/enable``     — operator toggles
- ``POST /apps/{id}/disable``
- ``PUT  /apps/{id}/config``     — validated vs manifest.params
- ``GET  /apps/{id}/status``     — proxies the app's /health + /state
"""

import ipaddress
import logging
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.auth import get_current_active_user, get_current_superuser, verify_token
from core.database import get_db
from core.permissions import RequirePermission
from models import AppInstallIntent, InstalledApp, User
from services.app_keys import (
    AppPrincipal, issue_key, looks_like_app_key,
    resolve_app_key, revoke_key,
)
from services.audit_service import write_audit_log
from services.app_entitlements import (
    entitlement_view, may_enable, store_license_key, verify_with_app,
)

# The one-click install/uninstall endpoints require this RBAC permission
# (in addition to the APPS_INSTALL_ENABLED opt-in). Reused as a FastAPI
# dependency on each mutating route below.
require_apps_install = RequirePermission("apps.install")

router = APIRouter(prefix="/apps", tags=["apps"])

logger = logging.getLogger(__name__)

# Seconds allowed for each proxied call to the app's /health and /state.
STATUS_PROBE_TIMEOUT_S = 3.0
# Actions can do real work (a footage query over SQLite); more generous
# than a health probe, still bounded so a hung app can't pin a worker.
ACTION_PROXY_TIMEOUT_S = 10.0


# ── App Store index (the "discover" half of the catalog) ───────────
#
# GET /apps/index reads a curated, product-owned YAML of installable
# apps (server/config/apps_index.yml) — the browse-and-install door,
# distinct from the installed_apps registry that /apps lists. The
# loader mirrors ai_models._load_use_case_map exactly: curated yaml →
# lru_cache → validated pydantic. Cross-referencing against
# installed_apps happens per request in the route, not in the cache.

APPS_INDEX_PATH = Path(__file__).resolve().parent.parent / "config" / "apps_index.yml"


class InstallSpec(BaseModel):
    """The exact copy-paste an operator runs to install one app: a
    docker-compose service block and the ``docker compose ... up``
    command that brings it up. No secrets — compose references
    ``${INTERNAL_API_KEY}`` from the operator's .env."""

    compose: str
    command: str


class IndexEntry(BaseModel):
    """One installable app in the store index — seeded from a shipped
    example's AppManifest (id/name/summary/category/version/
    requires_tasks/emits) plus store-only fields (image, docs_url,
    install). ``build_context`` is present while app images aren't yet
    published to GHCR (see apps_index.yml header) and is optional so it
    can drop out once they are; it is intentionally NOT part of the
    frontend response contract below."""

    id: str
    name: str
    summary: str
    category: str
    version: str
    # "installable" (default): a shipped image + compose service the
    # one-click installer applies. "external": a listing that links out
    # — a third-party app distributed elsewhere; the catalog shows
    # "Learn more" and never offers Install.
    kind: str = "installable"
    image: str | None = None
    external_url: str | None = None
    # Commerce, mirroring the manifest: free | paid | subscription |
    # contact, a human price line, and whether enabling needs a licence.
    pricing: str = "free"
    price_note: str = ""
    entitlement: str = "none"
    author: str = ""
    # Catalog curation (reviewer-set, never by the submitter's manifest):
    # ``verified`` = the maintainers vouch for the author (identity
    # confirmed, image reproducible from the linked source); ``featured``
    # = shown in the Featured row at the top of the catalog.
    verified: bool = False
    featured: bool = False
    requires_tasks: list[str] = []
    # RFC-0002 Phase 3 (decision 7): KAI-C adapters that must be
    # provisioned with the app; the reconciler ups + refcounts them.
    requires_adapters: list[str] = []
    emits: list[str] = []
    docs_url: str
    install: InstallSpec | None = None
    build_context: str | None = None
    # Optional sha256 digest the reconciler pins the image to. When
    # present, the one-click installer deploys ``image@sha256:...`` for
    # supply-chain integrity; when absent, the reconciler logs a loud
    # "UNPINNED — dev only" warning. Not for production without a digest.
    image_digest: str | None = None

    @model_validator(mode="after")
    def _shape_by_kind(self) -> "IndexEntry":
        if self.kind not in ("installable", "external"):
            raise ValueError("kind must be 'installable' or 'external'")
        if self.kind == "installable" and (not self.image or self.install is None):
            raise ValueError("installable entries need image + install")
        if self.kind == "external" and not (self.external_url or "").startswith("https://"):
            raise ValueError("external entries need an https:// external_url")
        if self.pricing not in ("free", "paid", "subscription", "contact"):
            raise ValueError("pricing must be free | paid | subscription | contact")
        if self.entitlement not in ("none", "license_key"):
            raise ValueError("entitlement must be none | license_key")
        return self


@lru_cache(maxsize=1)
def _load_apps_index() -> list[IndexEntry]:
    """Load the curated index, skipping invalid ENTRIES.

    One malformed entry (a bad community merge that slipped past
    scripts/validate_apps_index.py) must degrade to "that card is
    missing + an error log" — not 500 the whole store page AND every
    install endpoint. A missing/unparseable FILE still raises: that's a
    broken deploy, not a broken entry."""
    raw = yaml.safe_load(APPS_INDEX_PATH.read_text()) or []
    entries: list[IndexEntry] = []
    for item in raw:
        try:
            entries.append(IndexEntry(**item))
        except Exception:
            bad_id = item.get("id") if isinstance(item, dict) else None
            logger.error(
                "apps_index.yml: skipping invalid entry %r — run "
                "scripts/validate_apps_index.py",
                bad_id,
                exc_info=True,
            )
    return entries


# ── Config validation (pure, reusable by the SDK conformance kit) ──

# Manifest param "type" names → Python types accepted on the wire.
# "float" accepts int too (JSON has one number type); bool is excluded
# from the numeric checks because bool is a subclass of int in Python.
_PRIMITIVE_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
}


def _value_matches_type(value: Any, type_name: str) -> bool:
    """True when ``value`` is acceptable for a manifest param ``type``.

    Dotted UI-schema types (``"geometry.polygon"``) are list-shaped on
    the wire — deep validation is the catalog zone editor's job, so we
    only require a list. Unknown plain type names are not blocked.
    """
    if "." in type_name:
        return isinstance(value, list)
    expected = _PRIMITIVE_TYPES.get(type_name)
    if expected is None:
        return True
    if type_name in ("int", "float") and isinstance(value, bool):
        return False
    return isinstance(value, expected)


def validate_app_config(manifest: dict, config: dict) -> list[str]:
    """Validate operator ``config`` against ``manifest["params"]``.

    Returns a list of human-readable error strings (empty ⇒ valid):

    - config keys not declared in the manifest;
    - params with ``required=True`` and no default that are absent;
    - values whose type doesn't match the param ``type`` name;
    - ``per_camera=True`` params must be a dict keyed by camera id
      whose values each pass the type check.
    """
    errors: list[str] = []
    params: dict[str, dict] = {
        p["name"]: p
        for p in (manifest.get("params") or [])
        if isinstance(p, dict) and "name" in p
    }

    unknown = sorted(set(config) - set(params))
    if unknown:
        errors.append(f"unknown config keys: {', '.join(unknown)}")

    for name, param in params.items():
        if name not in config:
            if param.get("required") and param.get("default") is None:
                errors.append(f"missing required param: {name}")
            continue

        value = config[name]
        type_name = str(param.get("type", ""))

        if param.get("per_camera"):
            if not isinstance(value, dict):
                errors.append(
                    f"param '{name}' is per_camera and must be a dict "
                    "keyed by camera id"
                )
                continue
            for camera_id, camera_value in value.items():
                if not _value_matches_type(camera_value, type_name):
                    errors.append(
                        f"param '{name}' for camera '{camera_id}' must be "
                        f"of type {type_name}"
                    )
            continue

        if not _value_matches_type(value, type_name):
            errors.append(f"param '{name}' must be of type {type_name}")

    return errors


def _per_camera_param_names(manifest: dict) -> set[str]:
    return {
        p["name"]
        for p in (manifest.get("params") or [])
        if isinstance(p, dict) and "name" in p and p.get("per_camera")
    }


def _scope_per_camera_config(manifest: dict, config: dict,
                             scope: set[int] | None) -> dict:
    """``config`` with every ``per_camera`` param trimmed to the camera
    keys inside ``scope`` (None = unrestricted)."""
    if scope is None:
        return config
    from services.camera_scope import in_scope

    out = dict(config)
    for name in _per_camera_param_names(manifest):
        value = out.get(name)
        if isinstance(value, dict):
            out[name] = {k: v for k, v in value.items() if in_scope(scope, k)}
    return out


def _merge_scoped_config(manifest: dict, stored: dict, incoming: dict,
                         manage_scope: set[int] | None) -> tuple[dict, list[str]]:
    """Apply a NON-superuser's ``incoming`` config on top of ``stored``.

    Returns ``(merged, denied)``. ``denied`` names what they may not
    change — a site-wide (non per-camera) key whose value differs, or
    a per-camera entry for a camera outside ``manage_scope`` that
    differs — and a non-empty list means the write is refused whole.

    Absence is NOT a change here: a per-camera key left out of the
    payload is untouched, and within a key the caller read a config
    trimmed to their cameras (``_scope_per_camera_config``), so a
    camera missing from their payload is one they never saw and its
    stored entry is kept. Only a camera they can manage is removed by
    omission from a key they DID send — that is them erasing their own
    zone.
    """
    from services.camera_scope import in_scope

    per_camera = _per_camera_param_names(manifest)
    merged = dict(stored)
    denied: list[str] = []
    for key in sorted(set(stored) | set(incoming)):
        if key not in per_camera:
            if key in incoming and incoming[key] != stored.get(key):
                denied.append(f"'{key}' (site-wide setting)")
            continue
        if key not in incoming:
            continue            # untouched: the stored entries stand
        before = stored.get(key) if isinstance(stored.get(key), dict) else {}
        after = incoming.get(key) if isinstance(incoming.get(key), dict) else {}
        result = dict(before)
        for cam in sorted(set(before) | set(after), key=str):
            if in_scope(manage_scope, cam):
                if cam in after:
                    result[cam] = after[cam]
                else:
                    result.pop(cam, None)
            elif cam in after and after[cam] != before.get(cam):
                denied.append(f"'{key}' for camera '{cam}'")
        merged[key] = result
    return merged, denied


# ── App URL sovereignty guard (SSRF) ───────────────────────────────

# RFC1918 private ranges — a single-box / LAN deployment's app
# containers live here (or on loopback); nothing else is a legitimate
# place for an SDK app to be reached from this server.
_RFC1918_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def validate_app_url(url: str) -> str | None:
    """Return an operator-facing error string if ``url`` is not a safe
    app base URL, or ``None`` when it is acceptable.

    ``GET /apps/{id}/status`` server-side fetches ``{url}/health`` and
    ``{url}/state`` and reflects the bodies to the caller, so an
    arbitrary registered URL is an SSRF primitive. This guard mirrors
    the V-022 sovereignty precedent in ``kai-c/kai_c/sovereignty.py``
    (adapters must live on this machine / the operator's own network):

    - scheme must be ``http`` or ``https`` and a hostname is required;
    - IP literals: loopback (``127/8``, ``::1``) and RFC1918 private
      (``10/8``, ``172.16/12``, ``192.168/16``) are allowed; link-local
      ``169.254/16`` (cloud metadata) and all public addresses are
      refused;
    - hostnames: ``localhost`` and single-label Docker service names
      (no dots, e.g. ``loitering``) are allowed; dotted FQDNs are
      refused.

    Pure — no DNS resolution, so the answer can't be spoofed by a
    resolver and the check is safe to run per request.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"app url {url!r}: scheme must be http or https"
    host = parsed.hostname
    if not host:
        return f"app url {url!r}: hostname is required"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A name, not an IP literal: localhost or a single-label Docker
        # service name only. Dotted FQDNs point off-box.
        if host == "localhost" or "." not in host:
            return None
        return (
            f"app url {url!r}: host {host!r} is a dotted hostname; only "
            "localhost, single-label Docker service names, loopback, or "
            "RFC1918 private IPs are allowed"
        )

    if ip.is_link_local:
        return (
            f"app url {url!r}: host {host!r} is link-local "
            "(169.254/16 — cloud metadata range) and is refused"
        )
    if ip.is_loopback:
        return None
    if ip.version == 4 and any(ip in net for net in _RFC1918_NETS):
        return None
    return (
        f"app url {url!r}: host {host!r} is not a loopback or RFC1918 "
        "private address"
    )


# ── Registration auth (user JWT or service key) ────────────────────

# Registration is a service-to-service call: SDK apps boot with only
# the deployment's INTERNAL_API_KEY (the same secret adapters use
# against KAI-C), not a user JWT. ``POST /apps/register`` therefore
# accepts EITHER a normal user bearer token OR an ``X-Internal-Api-Key``
# header matching ``settings.internal_api_key``. Every other route
# (enable/disable/config/status) is an operator action and stays
# strictly user-authenticated via ``get_current_active_user``.

# auto_error=False: a missing Authorization header must fall through to
# the service-key check instead of short-circuiting with a 403.
_optional_bearer = HTTPBearer(auto_error=False)


def _internal_api_key() -> str:
    """The shared secret SDK apps send in ``X-Internal-Api-Key``. Read
    lazily from settings (same pattern as
    ``services.kai_c_service.KaiCService._internal_api_key``) so tests
    and dev setups that mutate the environment late still see it."""
    try:
        from core.config import settings

        return settings.internal_api_key or ""
    except Exception:
        import os

        return os.environ.get("INTERNAL_API_KEY", "")


def get_register_principal(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: Session = Depends(get_db),
) -> User | AppPrincipal | None:
    """Authenticate a registration call.

    Returns the ``User`` for the JWT path, or ``None`` for the
    service-key path (audit-logged as the ``app-sdk`` service
    identity). Raises 401 when neither credential is valid.
    """
    return _service_or_user_principal(x_internal_api_key, credentials, db)


def _service_or_user_principal(
    x_internal_api_key: str | None,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> User | AppPrincipal | None:
    """Shared body of the two service-capable principals.

    Three credential kinds, checked in this order:

    * an **app key** (``oak_…``, in either header) → :class:`AppPrincipal`
      for that app — it may read its own registry rows and re-register;
    * the deployment's ``INTERNAL_API_KEY`` → ``None`` (platform
      service identity, unscoped);
    * a user JWT → the ``User``.
    """
    bearer = credentials.credentials if credentials is not None else None
    for candidate in (x_internal_api_key, bearer):
        if looks_like_app_key(candidate):
            row = resolve_app_key(db, candidate)
            if row is not None:
                return AppPrincipal(app_id=row.id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked app key",
            )

    if x_internal_api_key is not None:
        expected = _internal_api_key()
        if expected and secrets.compare_digest(x_internal_api_key, expected):
            return None  # service identity
        # The SDK sends its one token as BOTH headers (it can't know
        # which kind the operator provisioned) — a non-matching key
        # only fails the request when there's no bearer to fall back to.
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid internal API key",
            )

    if credentials is not None:
        token_data = verify_token(credentials.credentials)
        if token_data is not None:
            user = (
                db.query(User)
                .filter(User.username == token_data.username)
                .first()
            )
            if user is not None and user.is_active:
                return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide a bearer token, an app key, or X-Internal-Api-Key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _own_app_only(principal, app_id: str) -> None:
    """An app key reads ITS OWN registry rows and nothing else."""
    if isinstance(principal, AppPrincipal) and principal.app_id != app_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An app key may only access its own app",
        )


def get_read_principal(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-Api-Key"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: Session = Depends(get_db),
) -> User | AppPrincipal | None:
    """Authenticate a READ-ONLY registry call (``GET /apps`` and
    ``GET /apps/{id}/status``).

    Mirrors :func:`get_register_principal` exactly — same constant-time,
    empty-key-safe compare, same JWT fallback — but is a *separate*
    dependency so it reads clearly at the call sites as "reads only".
    A registered SDK app / a companion service (e.g. the OpenNVR Agent)
    needs to *see* the catalog and probe an app's health to relay its
    state conversationally; it holds only the deployment's
    ``INTERNAL_API_KEY``, not a user JWT.

    Returns the ``User`` for the JWT path, or ``None`` for the
    service-key path. Raises 401 when neither credential is valid. The
    write routes (enable/disable/config/register) never call this — they
    stay strictly ``get_current_active_user`` (register additionally
    accepts the key via :func:`get_register_principal`).
    """
    return _service_or_user_principal(x_internal_api_key, credentials, db)



# ── Request schemas / serialization ────────────────────────────────


class AppRegisterRequest(BaseModel):
    """What the SDK POSTs on boot: where the app lives + its manifest."""

    url: str
    manifest: dict[str, Any]
    # The SDK's own version, for the compatibility line in the response
    # and the audit row. Optional: older SDKs send nothing.
    sdk_version: str | None = None
    # "I hold no app key" — an app booting without a persisted key (no
    # volume, first run) registers with the site key and asks for one;
    # the registry (re)issues it. An app registering WITH its own key
    # never gets a new one back.
    wants_key: bool = False


#: The app-registry contract version the SDK negotiates against, and
#: the oldest SDK this server still speaks to. Bump API_VERSION on any
#: change to the register/config/state/actions shapes; bump
#: MIN_SDK_VERSION only when an old SDK would misbehave, not merely
#: miss a feature.
API_VERSION = "1.2"
MIN_SDK_VERSION = "0.2.0"


def _serialize_app(row: InstalledApp) -> dict[str, Any]:
    """The wire shape of one installed app (list + register response)."""
    return {
        "id": row.id,
        "name": row.name,
        "category": row.category,
        "version": row.version,
        "url": row.url,
        "enabled": bool(row.enabled),
        "status": row.status,
        "last_seen": row.last_seen,
        "manifest": row.manifest_json,
        "config": row.config_json or {},
        # Credential state only — the key itself is returned once, at issue.
        "has_api_key": bool(row.api_key_hash),
        "api_key_issued_at": row.api_key_issued_at,
        # Licence verdict (never the key) — services/app_entitlements.py.
        "entitlement": entitlement_view(row),
    }


def _get_app_or_404(db: Session, app_id: str) -> InstalledApp:
    row = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"App '{app_id}' is not registered",
        )
    return row


# ── Routes ─────────────────────────────────────────────────────────


@router.get("")
async def list_apps(
    principal: User | None = Depends(get_read_principal),
    db: Session = Depends(get_db),
):
    """
    List all installed apps — the catalog's data source.

    Each card checks ``manifest.requires_tasks`` against
    ``GET /api/v1/adapters`` client-side to grey out apps whose model
    prerequisites aren't met.

    Requires an authenticated user OR the deployment's internal API key
    (``X-Internal-Api-Key``) — a service (the OpenNVR Agent) reads the
    catalog to relay installed apps as conversational skills. Read only;
    see :func:`get_read_principal`.
    """
    rows = db.query(InstalledApp).order_by(InstalledApp.id).all()
    return [_serialize_app(row) for row in rows]


@router.get("/index")
async def get_apps_index(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    The App Store "discover" listing — the browse-and-install catalog.

    Loads the curated ``apps_index.yml`` (product-owned editorial, one
    entry per shipped example) and cross-references the ``installed_apps``
    registry so each entry carries ``installed`` (an installed_apps row
    with that id exists) and ``enabled`` (that row's flag, or ``null``
    when not installed). The UI shows "installed" vs "available to
    install" from this alone.

    Never 404s — an entry that isn't installed simply has
    ``installed=false, enabled=null``. The index yaml ships with the
    server, so a load/validation failure is a 500 (a broken deploy),
    not an empty list.

    Requires authenticated user.
    """
    try:
        entries = _load_apps_index()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load apps index: {e!s}",
        )

    installed = {row.id: row for row in db.query(InstalledApp).all()}

    apps: list[dict[str, Any]] = []
    for entry in entries:
        row = installed.get(entry.id)
        apps.append(
            {
                "id": entry.id,
                "name": entry.name,
                "summary": entry.summary,
                "category": entry.category,
                "version": entry.version,
                "kind": entry.kind,
                "image": entry.image,
                "external_url": entry.external_url,
                "pricing": entry.pricing,
                "price_note": entry.price_note,
                "entitlement": entry.entitlement,
                "author": entry.author,
                "verified": entry.verified,
                "featured": entry.featured,
                "requires_tasks": entry.requires_tasks,
                "emits": entry.emits,
                "docs_url": entry.docs_url,
                "install": {
                    "compose": entry.install.compose,
                    "command": entry.install.command,
                } if entry.install is not None else None,
                "installed": row is not None,
                "enabled": bool(row.enabled) if row is not None else None,
            }
        )
    return {"apps": apps}


@router.post("/register")
async def register_app(
    request: AppRegisterRequest,
    principal: User | None = Depends(get_register_principal),
    db: Session = Depends(get_db),
):
    """
    Register (or re-register) an app — the SDK calls this on boot.

    Upserts by manifest id: url / manifest / name / version / category
    refresh on every boot, while the operator-owned ``enabled`` flag
    and ``config_json`` survive restarts.

    The app URL must pass :func:`validate_app_url` — /status later
    server-side fetches it, so off-box URLs are refused here (SSRF).

    Requires an authenticated user OR the deployment's internal API
    key (``X-Internal-Api-Key``) — registration is service-to-service;
    see :func:`get_register_principal`.
    """
    manifest = request.manifest
    missing = [key for key in ("id", "name", "version") if not manifest.get(key)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Manifest is missing required fields: {', '.join(missing)}",
        )
    # An app key registers only the app it was minted for.
    _own_app_only(principal, str(manifest["id"]))

    url_error = validate_app_url(request.url)
    if url_error is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=url_error,
        )

    app_id = str(manifest["id"])
    row = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    created = row is None
    if created:
        row = InstalledApp(id=app_id, enabled=False, config_json={})
        db.add(row)

    row.name = str(manifest["name"])
    row.version = str(manifest["version"])
    row.category = manifest.get("category")
    row.url = request.url.rstrip("/")
    row.manifest_json = manifest
    row.status = "registered"
    row.last_seen = datetime.now(UTC)
    # Per-app credential: issued on first registration, and re-issued
    # whenever the app registers with the SITE key (or a user) and says
    # it holds no key of its own (a fresh container with no persisted
    # key). An app presenting its own key keeps it. Audited below.
    issued_key: str | None = None
    if not isinstance(principal, AppPrincipal) and (
            created or not row.api_key_hash or request.wants_key):
        issued_key = issue_key(db, row)
    db.commit()
    db.refresh(row)

    actor_user = principal if isinstance(principal, User) else None
    registered_by = (
        f"user:{principal.username}" if isinstance(principal, User)
        else f"app:{principal.app_id}" if isinstance(principal, AppPrincipal)
        else "service:internal-api-key"
    )
    write_audit_log(
        db,
        action="app.register",
        # Service-key registrations have no user row; the actor is
        # recorded in details instead so the audit trail stays whole.
        user_id=actor_user.id if actor_user is not None else None,
        entity_type="app",
        entity_id=app_id,
        details={
            "created": created,
            "url": row.url,
            "version": row.version,
            "sdk_version": request.sdk_version,
            "registered_by": registered_by,
            "key_issued": issued_key is not None,
        },
    )
    # RFC-0002 Phase 5: event-scope grants. v1 policy is grant-on-
    # registration — but every grant is a distinct audit row (the RFC's
    # "every grant audited"), the manifest snapshot makes grants
    # catalog-visible, and the audit trail is what a future deny policy
    # or bus-side enforcement (per-app NATS users) will reconcile from.
    scopes = manifest.get("requires_scopes") or []
    if isinstance(scopes, list):
        for scope in scopes:
            if not isinstance(scope, str) or not scope.strip():
                continue
            write_audit_log(
                db,
                action="app.scope_granted",
                user_id=actor_user.id if actor_user is not None else None,
                entity_type="app",
                entity_id=app_id,
                details={"scope": scope.strip(), "policy": "grant-on-registration"},
            )
    out = _serialize_app(row)
    out["registry"] = _registry_info()
    if issued_key is not None:
        # The only time the key exists in the clear. The SDK persists it.
        out["api_key"] = issued_key
    return out


def _registry_info() -> dict[str, Any]:
    try:
        from main import __version__ as server_version  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — tests import the router alone
        server_version = "unknown"
    return {
        "server_version": server_version,
        "api_version": API_VERSION,
        "min_sdk_version": MIN_SDK_VERSION,
    }


@router.post("/{app_id}/enable")
async def enable_app(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """
    Enable an app. 404 if the app was never registered.

    Superuser only — turning a site-wide app on is a site decision.
    A licensed app (manifest ``entitlement: license_key``) also needs
    a key the app has accepted — 402 otherwise, with the reason.
    """
    row = _get_app_or_404(db, app_id)
    allowed, reason = may_enable(row)
    if not allowed:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED,
                            detail=f"Cannot enable '{app_id}': {reason}")
    row.enabled = True
    db.commit()
    db.refresh(row)

    write_audit_log(
        db,
        action="app.enable",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
    )
    return _serialize_app(row)


@router.post("/{app_id}/disable")
async def disable_app(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """
    Disable an app. 404 if the app was never registered.

    Superuser only — turning a site-wide app off silences it for
    every operator's cameras, not just the caller's.
    """
    row = _get_app_or_404(db, app_id)
    row.enabled = False
    db.commit()
    db.refresh(row)

    write_audit_log(
        db,
        action="app.disable",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
    )
    return _serialize_app(row)


@router.get("/{app_id}/config")
async def get_app_config(
    app_id: str,
    principal: User | None = Depends(get_read_principal),
    db: Session = Depends(get_db),
):
    """
    Read one app's effective config from the registry.

    This is the poll target for the SDK's LIVE CONFIG DELIVERY: the
    registry is the single source of truth (spec §05) and the running
    app polls this endpoint (with the deployment's internal key it
    already holds for registration) to apply operator edits made in the
    catalog's config form WITHOUT a container restart.

    Read-only, hence ``get_read_principal`` (user JWT **or**
    ``X-Internal-Api-Key``): the app itself is the intended service
    caller. Writes stay user-JWT-only (``PUT`` below).
    """
    _own_app_only(principal, app_id)
    row = _get_app_or_404(db, app_id)
    config = row.config_json or {}
    if isinstance(principal, User) and not principal.is_superuser:
        # A user sees the site-wide settings (read-only for them) but
        # only THEIR cameras' per-camera entries — a zone drawn on a
        # camera they were never assigned is not theirs to look at.
        from services.camera_scope import visible_camera_ids

        config = _scope_per_camera_config(
            row.manifest_json or {}, config,
            visible_camera_ids(db, principal))
    return {
        "id": row.id,
        "config": config,
        "updated_at": row.updated_at,
        # The licence verdict rides the live config poll so the app can
        # feature-gate itself (ContractMixin.entitlement).
        "entitlement": entitlement_view(row),
    }


@router.put("/{app_id}/config")
async def update_app_config(
    app_id: str,
    config: dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Replace an app's config, validated against ``manifest.params``.

    Because the manifest is typed and declarative, this endpoint needs
    zero app-specific code — see :func:`validate_app_config`.

    Manifest defaults are materialized into the stored config: any
    param omitted from the payload whose manifest ``default`` is not
    None is filled in before persisting (provided values are never
    overwritten), so ``config_json`` is the effective config and the
    registry stays the single source of truth (spec §05).

    Authorization: a superuser may change anything. Any other user may
    change ONLY the ``per_camera`` entries (zones, tripwires, per-camera
    limits) of cameras they can manage; a site-wide key or another
    camera's entry arriving CHANGED is a 403, and entries for cameras
    they cannot see (trimmed from their read) are preserved. The global
    settings of an app (its watchlists, thresholds, alarm policy) are a
    site decision, not a per-operator one.
    """
    row = _get_app_or_404(db, app_id)
    manifest = row.manifest_json or {}
    if not current_user.is_superuser:
        from services.camera_scope import manageable_camera_ids

        config, denied = _merge_scoped_config(
            manifest, row.config_json or {}, config,
            manageable_camera_ids(db, current_user))
        if denied:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=("Only a superuser can change "
                        + ", ".join(denied)),
            )
    errors = validate_app_config(manifest, config)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid config: {'; '.join(errors)}",
        )

    effective = dict(config)
    for param in manifest.get("params") or []:
        if not (isinstance(param, dict) and "name" in param):
            continue
        name = param["name"]
        if name not in effective and param.get("default") is not None:
            effective[name] = param["default"]

    row.config_json = effective
    db.commit()
    db.refresh(row)

    write_audit_log(
        db,
        action="app.config.update",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
        details={"config_keys": sorted(effective)},
    )
    return _serialize_app(row)


@router.post("/{app_id}/actions/{action_name}")
async def invoke_app_action(
    app_id: str,
    action_name: str,
    params: dict[str, Any] = Body(default_factory=dict),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Invoke one manifest-declared action on the app's contract surface.

    GOVERNANCE: **user-JWT only** — deliberately NOT ``get_read_principal``.
    Actions are operator verbs (search footage, enroll a face); the
    OpenNVR Agent's service key must never satisfy THIS route. The full
    boundary is layered (see ``Action`` in the SDK's manifest module):
    this JWT gate on the proxy, the deployment-key check on the app's
    own POST surface, and the agent shipping no HTTP-POST tool at all.
    Do not widen this dependency.

    Gates, in order: the app must be registered AND enabled; the action
    must be DECLARED in the stored manifest (404 otherwise — the
    manifest is the single source of truth for operator verbs); params
    are validated against the declared ``Action.params`` exactly like
    ``PUT /config`` validates against ``params``; the stored URL is
    re-checked against :func:`validate_app_url` before any fetch. Every
    invocation writes an audit row. The app's response is returned
    verbatim (its 4xx/5xx map to 502 with the app's error detail).
    """
    row = _get_app_or_404(db, app_id)
    if not row.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"App '{app_id}' is disabled — enable it first",
        )
    manifest = row.manifest_json or {}
    declared = {
        a.get("name"): a
        for a in (manifest.get("actions") or [])
        if isinstance(a, dict) and a.get("name")
    }
    action = declared.get(action_name)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"App '{app_id}' declares no action '{action_name}' "
                "(manifest.actions is the source of truth)"
            ),
        )
    # Param validation — same typed-manifest mechanics as PUT /config.
    errors = validate_app_config({"params": action.get("params") or []}, params)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid action params: {'; '.join(errors)}",
        )
    # An action aimed at a camera (``camera_id`` / ``camera`` param —
    # open THIS gate, clear THIS zone) is a control on that camera:
    # the caller must be allowed to manage it. Actions with no camera
    # target (enroll a face, search footage) stay open to any user.
    if not current_user.is_superuser:
        from services.camera_scope import (
            camera_id_from_handle, manageable_camera_ids,
        )

        scope = None
        for key in ("camera_id", "camera"):
            target = params.get(key)
            if target is None:
                continue
            cam_id = camera_id_from_handle(target)
            if scope is None:
                scope = manageable_camera_ids(db, current_user)
            if cam_id is None or cam_id not in scope:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not permitted to control this camera",
                )

    base_url = row.url.rstrip("/")
    if validate_app_url(base_url) is not None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="App URL is blocked by the registry's URL policy",
        )

    write_audit_log(
        db,
        action="app.action.invoke",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
        details={
            "actor": current_user.username,
            "action": action_name,
            # Param KEYS only — values may hold operator search terms
            # or personal data that doesn't belong in the audit log.
            "param_keys": sorted(params.keys()),
        },
    )

    # The app's action surface is key-gated (the SDK requires the
    # deployment token on its only write endpoint) — forward it. The
    # JWT gate above remains the operator check; this key is transport
    # auth between server and app.
    from core.config import settings

    headers = (
        {"X-Internal-Api-Key": settings.internal_api_key}
        if settings.internal_api_key
        else {}
    )
    # Who is invoking, and which cameras they may see/manage — signed
    # for this app (services/app_user_context.py) so it can render or
    # refuse per user without a login of its own.
    from services.app_user_context import user_context_headers

    headers.update(user_context_headers(db, row, current_user, purpose="action"))
    async with httpx.AsyncClient(timeout=ACTION_PROXY_TIMEOUT_S) as client:
        try:
            resp = await client.post(
                f"{base_url}/actions/{action_name}",
                json=params,
                headers=headers,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"App unreachable: {exc}",
            )
    if resp.status_code >= 400:
        detail: Any
        try:
            detail = resp.json()
        except Exception:  # noqa: BLE001
            detail = resp.text[:300]
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"app_status": resp.status_code, "app_error": detail},
        )
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="App returned a non-JSON action response",
        )


class LicenseIn(BaseModel):
    license_key: str


@router.put("/{app_id}/license")
async def put_app_license(
    app_id: str,
    payload: LicenseIn,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Store a licence key (encrypted, never returned) and ask the app
    to verify it. Superuser only. Returns the verdict; enabling the app
    is refused until it is ``valid``."""
    row = _get_app_or_404(db, app_id)
    key = payload.license_key.strip()
    if not key or len(key) > 2000:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="license_key must be 1..2000 characters")
    store_license_key(row, key)
    db.commit()
    view = await verify_with_app(row)
    db.commit()
    write_audit_log(db, action="app.license.set", user_id=current_user.id,
                    entity_type="app", entity_id=app_id,
                    details={"status": view["status"], "plan": view["plan"]})
    return {"id": app_id, "entitlement": view}


@router.post("/{app_id}/license/verify")
async def verify_app_license(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Re-ask the app about the stored key (after a renewal, or when the
    app was down when the key was entered)."""
    row = _get_app_or_404(db, app_id)
    view = await verify_with_app(row)
    db.commit()
    return {"id": app_id, "entitlement": view}


@router.delete("/{app_id}/license")
async def delete_app_license(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Forget the key. A licensed app that is enabled stays enabled
    until it is next toggled — revoking a key is not an outage."""
    row = _get_app_or_404(db, app_id)
    store_license_key(row, None)
    db.commit()
    write_audit_log(db, action="app.license.clear", user_id=current_user.id,
                    entity_type="app", entity_id=app_id)
    return {"id": app_id, "entitlement": entitlement_view(row)}


@router.post("/{app_id}/key/rotate")
async def rotate_app_key(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Mint a NEW app key and return it once; the old one stops working
    immediately. Superuser only. Hand the value to the app's operator
    (``OPENNVR_APP_KEY``), or restart the app with the site key and it
    asks for one itself (``wants_key``)."""
    row = _get_app_or_404(db, app_id)
    plain = issue_key(db, row)
    db.commit()
    write_audit_log(db, action="app.key.rotate", user_id=current_user.id,
                    entity_type="app", entity_id=app_id)
    return {"id": app_id, "api_key": plain,
            "api_key_issued_at": row.api_key_issued_at}


@router.delete("/{app_id}/key")
async def revoke_app_key(
    app_id: str,
    current_user: User = Depends(get_current_superuser),
    db: Session = Depends(get_db),
):
    """Revoke the app's key: it can no longer read its config, its
    roster, or re-register with that key. Superuser only."""
    row = _get_app_or_404(db, app_id)
    revoke_key(row)
    db.commit()
    write_audit_log(db, action="app.key.revoke", user_id=current_user.id,
                    entity_type="app", entity_id=app_id)
    return {"id": app_id, "has_api_key": False}


@router.get("/{app_id}/status")
async def get_app_status(
    app_id: str,
    principal: User | None = Depends(get_read_principal),
    db: Session = Depends(get_db),
):
    """
    Proxy the app's live ``/health`` and ``/state`` endpoints.

    Degrades gracefully: an unreachable app yields
    ``{"health": {"status": "unreachable"}, "state": None}`` rather
    than a 5xx, and the stored row status flips to ``unreachable`` so
    the catalog dot goes red without polling the app itself.

    The stored URL is re-checked against :func:`validate_app_url`
    before any fetch (defense in depth for rows registered before the
    guard existed): a blocked URL short-circuits to
    ``{"health": {"status": "blocked_url"}, "state": None}``.

    Requires an authenticated user OR the deployment's internal API key
    (``X-Internal-Api-Key``) — a service reads live app state to relay
    it; see :func:`get_read_principal`. Read only.
    """
    _own_app_only(principal, app_id)
    row = _get_app_or_404(db, app_id)
    base_url = row.url.rstrip("/")

    if validate_app_url(base_url) is not None:
        row.status = "blocked_url"
        db.commit()
        return {"health": {"status": "blocked_url"}, "state": None}

    health: dict[str, Any]
    state: Any = None
    reachable = False
    async with httpx.AsyncClient(timeout=STATUS_PROBE_TIMEOUT_S) as client:
        try:
            health_resp = await client.get(f"{base_url}/health")
            health_resp.raise_for_status()
            health = health_resp.json()
            reachable = True
        except Exception:
            health = {"status": "unreachable"}

        if reachable:
            try:
                state_resp = await client.get(f"{base_url}/state")
                state_resp.raise_for_status()
                state = state_resp.json()
            except Exception:
                state = None

    # SDK /health reports `ready` (spec §03); tolerate its absence on
    # a 200 rather than flagging a healthy app unreachable.
    ready = reachable and bool(health.get("ready", True))
    row.status = "ok" if ready else "unreachable"
    if reachable:
        row.last_seen = datetime.now(UTC)
    db.commit()

    if isinstance(principal, User) and not principal.is_superuser:
        # Live state is per-camera data (occupancy per zone, the LPR
        # review queue, each camera's last read): a user gets their
        # cameras' slice. The service-key path (the agent) is scoped
        # by its own roster, and a superuser sees the site.
        from services.camera_scope import filter_app_state, visible_camera_ids

        state = filter_app_state(state, visible_camera_ids(db, principal))

    return {"health": health, "state": state}


# ── One-click install: desired-state writes only (NO docker here) ──────
#
# SECURITY INVARIANT: the web app process never runs Docker, never holds
# the docker socket, and never spawns a subprocess for installs. These
# endpoints do exactly three things — (a) gate on the APPS_INSTALL_ENABLED
# opt-in + the apps.install RBAC permission, (b) validate the id against
# the curated index and copy image/digest FROM the index (never from user
# input), and (c) upsert one desired-state row in app_install_intents and
# audit it. A separate, minimally-privileged reconciler
# (scripts/app-installer) is the only component that applies the intent.
# See docs/APPS_INSTALL.md.


def _require_install_enabled() -> None:
    """403 unless the operator has opted into one-click install.

    Reads ``settings.apps_install_enabled`` directly. That setting has a
    hard default of ``False`` in ``core/config.py`` (the sovereign /
    air-gapped posture), so this gate is fail-closed by construction: a
    missing or unparsed value never lands here as "enabled". We import
    settings inside the function (not at module load) so tests toggling
    the flag late still see it, but we do NOT wrap the read in a broad
    ``except`` — a genuinely broken settings object should surface as an
    error, not be silently masked into an env-var fallback that could
    read as enabled. When off, the copy-paste command path
    (GET /apps/index) stays available; only server-side
    install/uninstall is disabled.
    """
    from core.config import settings

    if not bool(settings.apps_install_enabled):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "one-click install disabled; use the copy-paste command "
                "(set APPS_INSTALL_ENABLED=true to opt in)"
            ),
        )


def _index_entry_or_404(app_id: str) -> IndexEntry:
    """Return the curated index entry for ``app_id`` or 404.

    INDEX-ONLY guard: only an app present in apps_index.yml is
    installable. An id that isn't in the curated index is rejected here,
    so no arbitrary image / user input can ever reach the reconciler.
    """
    try:
        entries = _load_apps_index()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load apps index: {e!s}",
        )
    for entry in entries:
        if entry.id == app_id:
            if entry.kind != "installable":
                # A link-out listing has no image and no compose service:
                # nothing for the reconciler to apply.
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"App '{app_id}' is an external listing — install it "
                           f"from {entry.external_url}",
                )
            return entry
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"App '{app_id}' is not in the curated App Store index",
    )


def _serialize_intent(row: AppInstallIntent) -> dict[str, Any]:
    """Wire shape of one desired-state install intent."""
    return {
        "id": row.id,
        "image": row.image,
        "image_digest": row.image_digest,
        "desired": row.desired,
        "status": row.status,
        "message": row.message,
        "requested_by": row.requested_by,
        "requested_at": row.requested_at,
        "updated_at": row.updated_at,
    }


def _write_intent(
    db: Session,
    entry: IndexEntry,
    *,
    desired: str,
    actor: str,
) -> AppInstallIntent:
    """Upsert the desired-state row for ``entry`` and reset it to
    ``pending`` so the reconciler re-evaluates. image/image_digest are
    always taken from the curated index entry, never from the request."""
    def _apply(row: AppInstallIntent) -> AppInstallIntent:
        row.image = entry.image
        row.image_digest = entry.image_digest
        row.desired = desired
        row.status = "pending"
        row.message = None
        row.requested_by = actor
        row.requested_at = datetime.now(UTC)
        db.commit()
        db.refresh(row)
        return row

    row = (
        db.query(AppInstallIntent)
        .filter(AppInstallIntent.id == entry.id)
        .first()
    )
    if row is not None:
        return _apply(row)
    row = AppInstallIntent(id=entry.id)
    db.add(row)
    try:
        return _apply(row)
    except IntegrityError:
        # Two concurrent FIRST-TIME requests both saw "no row" and both
        # INSERTed; the PK rejects the loser. State is already correct
        # (the winner wrote the same desired data) — recover by updating
        # the winner's row instead of 500ing the losing click.
        db.rollback()
        row = (
            db.query(AppInstallIntent)
            .filter(AppInstallIntent.id == entry.id)
            .one()
        )
        return _apply(row)


@router.post("/index/{app_id}/install")
async def install_app(
    app_id: str,
    current_user: User = Depends(require_apps_install),
    db: Session = Depends(get_db),
):
    """
    Opt-in one-click install — write a DESIRED-STATE record only.

    Gated by BOTH ``APPS_INSTALL_ENABLED`` (403 when off — the sovereign
    default) AND the ``apps.install`` RBAC permission (via the dependency
    above). The app id must be in the curated ``apps_index.yml`` (404
    otherwise) — no arbitrary images. image/digest are copied from the
    index, never from the caller.

    This endpoint does NOT run Docker: it upserts one
    ``app_install_intents`` row with ``desired="installed"``,
    ``status="pending"`` and audits it. The ``scripts/app-installer``
    reconciler applies it.
    """
    _require_install_enabled()
    entry = _index_entry_or_404(app_id)

    row = _write_intent(
        db, entry, desired="installed", actor=current_user.username
    )

    write_audit_log(
        db,
        action="app.install.request",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
        details={
            "actor": current_user.username,
            "image": entry.image,
            "image_digest": entry.image_digest,
            "desired": "installed",
        },
    )
    return _serialize_intent(row)


@router.post("/index/{app_id}/uninstall")
async def uninstall_app(
    app_id: str,
    current_user: User = Depends(require_apps_install),
    db: Session = Depends(get_db),
):
    """
    Opt-in one-click uninstall — flip the desired state to ``absent``.

    Same gates as install (opt-in flag + apps.install + index-only).
    Writes ``desired="absent"``, ``status="pending"`` on the intent row
    and audits it; the reconciler brings the app's compose stack down.
    """
    _require_install_enabled()
    entry = _index_entry_or_404(app_id)

    row = _write_intent(
        db, entry, desired="absent", actor=current_user.username
    )

    # Drop the registration row now (best-effort) so the catalog moves
    # the card back to "Available to install" immediately instead of
    # showing an immortal zombie "installed" card drifting to
    # unreachable — nothing else ever deletes installed_apps rows on
    # uninstall (the reconciler only stops the container). If the
    # teardown fails and the app restarts, its boot-time re-register
    # simply recreates the row.
    reg = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    if reg is not None:
        db.delete(reg)
        db.commit()

    write_audit_log(
        db,
        action="app.uninstall.request",
        user_id=current_user.id,
        entity_type="app",
        entity_id=app_id,
        details={
            "actor": current_user.username,
            "image": entry.image,
            "image_digest": entry.image_digest,
            "desired": "absent",
        },
    )
    return _serialize_intent(row)


@router.get("/index/{app_id}/install-status")
async def get_install_status(
    app_id: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Report the desired-state + reconcile status for one curated app.

    Readable by any authenticated user (it's status, not a mutation).
    The id must be in the curated index (404 otherwise). When no intent
    has ever been written, returns ``desired=null, status="none"`` so the
    UI can distinguish "never requested" from "pending".
    """
    _index_entry_or_404(app_id)  # index-only, even for reads

    row = (
        db.query(AppInstallIntent)
        .filter(AppInstallIntent.id == app_id)
        .first()
    )
    if row is None:
        return {
            "id": app_id,
            "desired": None,
            "status": "none",
            "image": None,
            "image_digest": None,
            "message": None,
        }
    return _serialize_intent(row)


# Cap on proxied app-UI documents. An app dashboard is a small
# self-contained page; anything bigger is a bug or an abuse vector.
UI_PROXY_MAX_BYTES = 1_000_000


@router.get("/{app_id}/ui")
async def get_app_ui(
    app_id: str,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """Proxy the app's ``GET /ui`` dashboard (RFC-0002 Phase 4).

    The app-surface routing convention: an app that sets ``has_ui`` in
    its manifest serves ONE self-contained HTML document at ``/ui`` on
    its contract port, and this route is how the operator UI reaches it
    — apps never get their own public ports. Deliberately minimal:

    * **user-JWT only** (like actions): dashboards are operator surface;
      the service key must never satisfy this route.
    * ``/ui`` exactly — no subpaths, no assets, no methods but GET. A
      dashboard that needs more than one self-contained document should
      argue for widening the convention in an RFC, not around it.
    * the response is size-capped and always served as text/html; the
      catalog renders it inside a SANDBOXED iframe (no scripts), so
      pages should be static HTML/CSS.
    """
    row = _get_app_or_404(db, app_id)
    manifest = row.manifest_json if isinstance(row.manifest_json, dict) else {}
    if not manifest.get("has_ui"):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"App '{app_id}' declares no UI (manifest.has_ui).",
        )
    base_url = row.url.rstrip("/")
    if validate_app_url(base_url) is not None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="App URL is blocked by policy.",
        )
    # The viewer's identity and camera scope travel with the request
    # (signed for this app) so the page can be per-user.
    from services.app_user_context import user_context_headers

    headers = user_context_headers(db, row, current_user, purpose="ui")
    try:
        async with httpx.AsyncClient(timeout=STATUS_PROBE_TIMEOUT_S) as client:
            resp = await client.get(f"{base_url}/ui", headers=headers)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"App '{app_id}' is unreachable.",
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"App '{app_id}' answered {resp.status_code} for /ui.",
        )
    body = resp.content[:UI_PROXY_MAX_BYTES]
    from fastapi.responses import HTMLResponse

    return HTMLResponse(content=body.decode("utf-8", errors="replace"))
