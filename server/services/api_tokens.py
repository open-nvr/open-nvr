# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""API tokens: scoped, revocable credentials for non-browser clients (HA-101).

A token looks like ``onvr_<prefix>_<secret>``. Only ``sha256(token)`` is
stored; the prefix is public and indexes the row.

Security model. A token can never do more than its owner:

1. **Deny by default.** A token may call only the routes in
   :data:`TOKEN_ROUTES`, each mapped to the permission it needs. Everything
   else answers 403 for a token, whatever the route's own guard is. (The
   codebase guards ~117 routes with nothing more than "is logged in", so
   opting routes IN is the only safe way to give a token a narrow reach.)
2. **Two-sided permission check.** The route's permission must be in the
   token's ``scopes`` AND held by the owner (superuser / ``full_access``
   count). Revoking a permission from the owner's role revokes it from the
   token immediately.
3. **Camera gate.** Any camera named in the path or query must be in the
   token's allow-list. Endpoints that take a camera in the request BODY
   must call :func:`check_token_camera` themselves.
4. **Never a superuser.** The request runs as a read-only
   :class:`TokenPrincipal` whose ``is_superuser`` is always False, so every
   superuser-only route refuses it. The owner's ORM row is never modified:
   flipping ``is_superuser`` on it would be flushed by the next commit and
   demote the admin permanently.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

TOKEN_PREFIX = "onvr_"
#: Response header naming why a token request was refused, where a client
#: must act differently (contract.json ``errors``).
ERROR_HEADER = "X-OpenNVR-Error"
_TOKEN_RE = re.compile(r"^onvr_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{32,64})$")

#: Permissions a token may be granted. Never ``full_access``: a token is
#: always an enumerated, narrow grant.
ALLOWED_SCOPES: frozenset[str] = frozenset({
    "cameras.view", "cameras.manage", "live.view", "ptz.control",
    "recordings.view", "recordings.pause", "alerts.view", "alerts.manage",
    "events.create", "apps.actions", "settings.view",
    # Only ever used for PUT /site-mode (routes are opt-in, see TOKEN_ROUTES):
    # Home Assistant's alarm panel arms and disarms the site.
    "settings.manage",
    # App-declared entities (HA-114): read / drive an app's own entities.
    "apps.view",
})

_A = "/api/v1"

#: (METHOD, route template) -> permission required. The ONLY routes a token
#: can reach. Later issues add rows as they add token-facing endpoints; this
#: table is the written contract of what an API client can do.
TOKEN_ROUTES: dict[tuple[str, str], str] = {
    ("GET", f"{_A}/system/info"): "settings.view",
    ("GET", f"{_A}/system/resources"): "settings.view",
    ("GET", f"{_A}/cameras/"): "cameras.view",
    ("GET", f"{_A}/cameras/{{camera_id}}"): "cameras.view",
    ("GET", f"{_A}/cameras/{{camera_id}}/stats"): "cameras.view",
    ("GET", f"{_A}/cameras/{{camera_id}}/zones"): "cameras.view",
    ("GET", f"{_A}/live-state"): "cameras.view",
    # The route checks the kind's own permission and the camera.
    ("POST", f"{_A}/media/sign"): "cameras.view",
    ("GET", f"{_A}/site-mode"): "settings.view",
    ("PUT", f"{_A}/site-mode"): "settings.manage",
    # Each descriptor's own required_scope is checked by routers/entities.py.
    ("GET", f"{_A}/entities"): "cameras.view",
    ("GET", f"{_A}/entities/states"): "cameras.view",
    ("POST", f"{_A}/entities/{{key}}/command"): "cameras.view",
    # Recorded visits (main's plain-language search), scoped to the cameras.
    ("GET", f"{_A}/search"): "recordings.view",
    ("GET", f"{_A}/search/summary"): "cameras.view",
    # A frame to a caption/VQA model (HA-501): as seeing the camera live.
    ("POST", f"{_A}/cameras/{{camera_id}}/describe"): "live.view",
    # Only TOKEN_CAMERA_FIELDS may be changed (routers/cameras.update_camera).
    ("PUT", f"{_A}/cameras/{{camera_id}}"): "cameras.manage",
    # The route checks the site flag and recordings.pause itself.
    ("POST", f"{_A}/cameras/{{camera_id}}/recording"): "cameras.view",
    ("GET", f"{_A}/cameras/{{camera_id}}/snapshot"): "live.view",
    ("POST", f"{_A}/cameras/{{camera_id}}/ptz/move"): "ptz.control",
    ("POST", f"{_A}/cameras/{{camera_id}}/ptz/stop"): "ptz.control",
    ("GET", f"{_A}/streams/{{camera_id}}/info"): "live.view",
    # The ticket carries the token (routers/events.py), so the socket keeps
    # the token's cameras and scopes; see TOKEN_EVENT_SCOPES.
    ("POST", f"{_A}/events/ws-ticket"): "cameras.view",
    ("GET", f"{_A}/events"): "recordings.view",
    ("POST", f"{_A}/events"): "events.create",  # camera in the body: checked there
    ("PUT", f"{_A}/events/{{event_id}}/end"): "events.create",
    ("POST", f"{_A}/events/{{event_id}}/protect"): "recordings.view",
    ("GET", f"{_A}/cameras/{{camera_id}}/ptz/presets"): "ptz.control",
    ("POST", f"{_A}/cameras/{{camera_id}}/ptz/presets"): "ptz.control",
    ("POST", f"{_A}/cameras/{{camera_id}}/ptz/presets/{{preset_token}}/goto"): "ptz.control",
    ("GET", f"{_A}/alerts-inbox"): "alerts.view",
    ("GET", f"{_A}/alerts-inbox/{{alert_id}}/images/{{name}}"): "alerts.view",
    ("POST", f"{_A}/alerts-inbox/ack"): "alerts.manage",
    ("POST", f"{_A}/recordings/export/ticket"): "recordings.view",
    # A token mints a card's session token; checked further in the route.
    ("POST", f"{_A}/api-tokens/session"): "cameras.view",
}

#: What a dashboard-card session token may hold: reading only. Its parent's
#: scopes are intersected with this, so a card can never act.
SESSION_SCOPES: frozenset[str] = frozenset({
    "cameras.view", "live.view", "recordings.view", "alerts.view", "settings.view"})
#: Longest life of a session token, in seconds.
SESSION_MAX_TTL_S = 600
#: The only non-GET routes a session token may call: opening the events
#: socket and signing media for display. Nothing that changes state.
SESSION_WRITES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/v1/events/ws-ticket"), ("POST", "/api/v1/media/sign")})
#: Sessions a token may open per minute (a card reopens one about every eight).
SESSION_MINTS_PER_MINUTE = 30

#: Read-only API paths an integration may relay for a browser that cannot
#: reach OpenNVR itself (Home Assistant's card passthrough, design §7.8).
#: ``/system/info`` publishes it; GET only.
PASSTHROUGH_ALLOWLIST: tuple[str, ...] = (
    "/api/v1/cameras/", "/api/v1/live-state", "/api/v1/entities", "/api/v1/events",
    "/api/v1/alerts-inbox", "/api/v1/search", "/api/v1/site-mode", "/api/v1/system/info")

#: Camera fields a token may change through ``PUT /cameras/{id}``. Not the
#: stream source or credentials. ``is_active`` is added only while the
#: site allows pausing recording (turning a camera off stops recording).
#: ``reason`` is audit-only and always allowed.
TOKEN_CAMERA_FIELDS: frozenset[str] = frozenset({"detection_enabled"})

#: Query / path parameters that name a camera, checked by the camera gate.
_CAMERA_KEYS = ("camera_id", "cam_id", "camera")
_CAMERA_LIST_KEYS = ("camera_ids",)
_HANDLE_RE = re.compile(r"^cam-?(\d+)$")

#: last_used_at is written at most this often per token (write amplification).
LAST_USED_WRITE_INTERVAL_S = 60.0
_last_used_written: dict[int, float] = {}


# ── minting and lookup ────────────────────────────────────────────────────


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def looks_like_token(value: str | None) -> bool:
    return bool(value) and value.startswith(TOKEN_PREFIX)


def mint_token() -> tuple[str, str, str]:
    """A fresh token: ``(plain, prefix, sha256)``. The plain value is shown once."""
    prefix = secrets.token_hex(4)
    plain = f"{TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    return plain, prefix, hash_token(plain)


def _as_aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def resolve_token(db: Session, plain: str | None):
    """The live ApiToken row for *plain*, or None (unknown, revoked, expired)."""
    from models import ApiToken

    match = _TOKEN_RE.fullmatch(plain or "")
    if not match:
        return None
    row = db.query(ApiToken).filter(ApiToken.prefix == match.group(1)).first()
    if row is None or not secrets.compare_digest(row.token_hash, hash_token(plain)):
        return None
    return row if _row_live(row) else None


def _row_live(row) -> bool:
    if row.revoked_at is not None:
        return False
    expires = _as_aware(row.expires_at)
    return expires is None or expires > datetime.now(UTC)


# ── the principal ─────────────────────────────────────────────────────────


class TokenPrincipal:
    """Read-only stand-in for the owner while a token request runs.

    Attribute reads are delegated to the owner's User row (``id``,
    ``username``, ``role``, ...). ``is_superuser`` is always False. Writing
    any attribute raises: the owner's row must never be changed through a
    token, least of all ``is_superuser``.
    """

    __slots__ = ("_user", "_token_id", "_token_name", "scopes", "camera_ids")

    def __init__(self, user, token_id: int, token_name: str,
                 scopes: frozenset[str], camera_ids: frozenset[int] | None):
        object.__setattr__(self, "_user", user)
        object.__setattr__(self, "_token_id", token_id)
        object.__setattr__(self, "_token_name", token_name)
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "camera_ids", camera_ids)

    @property
    def is_superuser(self) -> bool:
        return False

    @property
    def user(self):
        """The owner's ORM row, for code that genuinely needs the model."""
        return self._user

    @property
    def token_id(self) -> int:
        return self._token_id

    @property
    def token_name(self) -> str:
        return self._token_name

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_user"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"TokenPrincipal is read-only (tried to set {name!r}); an API "
            "token must never modify its owner's user record"
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TokenPrincipal token={self._token_name!r} owner={self._user.username!r}>"


def is_token_principal(obj) -> bool:
    return isinstance(obj, TokenPrincipal)


# ── authorization of one request ──────────────────────────────────────────


def _owner_has(user, permission: str) -> bool:
    """Same rule as core.permissions.user_has_permission, for the OWNER
    (duplicated to avoid the core.auth <-> core.permissions import cycle)."""
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    names = {p.name for p in (getattr(role, "permissions", None) or [])}
    return "full_access" in names or permission in names


def token_has_permission(principal: TokenPrincipal, permission: str) -> bool:
    return permission in principal.scopes and _owner_has(principal.user, permission)


def describe_caller(db: Session, principal) -> dict[str, Any]:
    """Who is asking, as ``/system/info`` reports it. A token learns what it
    may actually do (its scopes that its owner also holds), its cameras and
    when it expires: the Home Assistant config flow lists missing scopes from
    this, and warns before the token runs out. Never the secret or hash."""
    if not is_token_principal(principal):
        return {"kind": "user", "username": getattr(principal, "username", None)}
    from models import ApiToken

    row = db.query(ApiToken).filter(ApiToken.id == principal.token_id).first()
    expires = _as_aware(row.expires_at) if row is not None else None
    return {
        "kind": "token",
        "name": principal.token_name,
        "scopes": sorted(s for s in principal.scopes if token_has_permission(principal, s)),
        "camera_ids": (None if principal.camera_ids is None
                       else sorted(principal.camera_ids)),
        "expires_at": expires.isoformat() if expires else None,
    }


def _route_key(request) -> tuple[str, str] | None:
    """(METHOD, full route template) of the matched route, or None.

    FastAPI < 0.140 stores the full template (``/api/v1/cameras/{camera_id}``)
    on ``scope["route"].path``; newer versions keep included routers nested,
    so ``.path`` is only the part below the include prefix. Rebuild the full
    template the same way on both: render the template with this request's
    path params, strip that suffix off the real path, and put the template
    back on the prefix left over. Anything that doesn't line up exactly
    returns None, which callers treat as "not a token route" (deny)."""
    scope = getattr(request, "scope", None) or {}
    route = scope.get("route")
    template = getattr(route, "path", None)
    if not template:
        return None
    convertors = getattr(route, "param_convertors", None) or {}
    rendered = template
    try:
        for name, value in (scope.get("path_params") or {}).items():
            conv = convertors.get(name)
            text = conv.to_string(value) if conv is not None else str(value)
            rendered = re.sub(r"\{" + re.escape(name) + r"(:[^}]*)?\}", lambda _m: text, rendered)
    except Exception:
        return None
    path = scope.get("path") or ""
    if "{" in rendered or not path.endswith(rendered):
        return None
    prefix = path[: len(path) - len(rendered)]
    root = scope.get("root_path") or ""
    if root and prefix.startswith(root):
        prefix = prefix[len(root):]
    return (request.method.upper(), prefix + template)


def _camera_ids_named(request) -> tuple[set[int], bool]:
    """Camera ids in the path/query, and whether any value was unparseable."""
    named: set[int] = set()
    bad = False
    params: list[tuple[str, str]] = list(getattr(request, "path_params", {}).items())
    params += list(request.query_params.multi_items())
    for key, value in params:
        values: list[str] = []
        if key in _CAMERA_KEYS:
            values = [str(value)]
        elif key in _CAMERA_LIST_KEYS:
            values = [v for v in str(value).split(",") if v.strip()]
        elif key == "path" and _HANDLE_RE.fullmatch(str(value)):
            values = [str(value)]
        for raw in values:
            raw = raw.strip()
            m = _HANDLE_RE.fullmatch(raw)
            if raw.isdigit():
                named.add(int(raw))
            elif m:
                named.add(int(m.group(1)))
            else:
                bad = True
    return named, bad


def check_token_camera(principal, camera_id: int | str | None) -> None:
    """403 unless *camera_id* is in the token's allow-list (no-op for users).

    For endpoints that take a camera in the request BODY, which the central
    gate in :func:`authorize_request` cannot see.
    """
    if not is_token_principal(principal) or principal.camera_ids is None:
        return
    raw = str(camera_id) if camera_id is not None else ""
    m = _HANDLE_RE.fullmatch(raw)
    cid = int(raw) if raw.isdigit() else (int(m.group(1)) if m else None)
    if cid is None or cid not in principal.camera_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This API token is not allowed to use that camera")


def _ip_allowed(ip: str, cidrs: list[str] | None) -> bool:
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


_session_mints: dict[int, list[float]] = {}


def allow_session_mint(parent_id: int) -> bool:
    """Rate limit per parent token: a card renews its session every ~8 min,
    so anything near the limit is a misbehaving client, not a dashboard."""
    now = time.monotonic()
    recent = [t for t in _session_mints.get(parent_id, []) if now - t < 60.0]
    if len(recent) >= SESSION_MINTS_PER_MINUTE:
        _session_mints[parent_id] = recent
        return False
    recent.append(now)
    _session_mints[parent_id] = recent
    return True


def _touch_last_used(token_id: int, ip: str) -> None:
    now = time.monotonic()
    if len(_last_used_written) > 1000:
        # Short-lived session tokens each add an id: forget the stale ones.
        for stale in [k for k, v in _last_used_written.items()
                      if now - v >= LAST_USED_WRITE_INTERVAL_S]:
            _last_used_written.pop(stale, None)
    last = _last_used_written.get(token_id)
    if last is not None and now - last < LAST_USED_WRITE_INTERVAL_S:
        return
    _last_used_written[token_id] = now
    try:
        from core.database import SessionLocal
        from models import ApiToken

        with SessionLocal() as s:
            s.query(ApiToken).filter(ApiToken.id == token_id).update(
                {"last_used_at": datetime.now(UTC), "last_used_ip": ip[:64] or None},
                synchronize_session=False,
            )
            s.commit()
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a request
        pass


def authorize_request(request, db: Session, plain: str) -> TokenPrincipal:
    """Resolve and authorize a token for THIS request, or raise 401/403."""
    from core.client_ip import get_client_ip
    from core.request_context import current as current_ctx
    from models import User

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid, expired or revoked API token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    row = resolve_token(db, plain)
    if row is None:
        raise unauthorized
    owner = db.query(User).filter(User.id == row.owner_user_id).first()
    if owner is None or not owner.is_active:
        raise unauthorized

    ip = get_client_ip(request) if request is not None else ""
    if not _ip_allowed(ip, row.allowed_cidrs):
        # Machine-readable, so a client can tell "fix the token's allowed
        # addresses" from "the token lacks a scope" (contract: errors).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This API token may not be used from this address",
                            headers={ERROR_HEADER: "token_address"})

    principal = _principal(owner, row)
    cameras = principal.camera_ids

    key = _route_key(request) if request is not None else None
    required = TOKEN_ROUTES.get(key) if key else None
    if required is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="This endpoint is not available to API tokens")
    if not token_has_permission(principal, required):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"This API token lacks the '{required}' permission")
    if row.parent_id is not None and key[0] != "GET" and key not in SESSION_WRITES:
        # A card's session reads. Its scopes are all "view" scopes, but a few
        # view-gated routes still change something (protecting footage).
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="A session token can only read")

    if cameras is not None:
        named, bad = _camera_ids_named(request)
        if bad or not named <= cameras:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="This API token is not allowed to use that camera")

    ctx = current_ctx()
    if ctx is not None:
        ctx.actor = f"token:{row.name}"
    _touch_last_used(row.id, ip)
    return principal


def _principal(owner, row) -> TokenPrincipal:
    scopes = frozenset(s for s in (row.scopes or []) if s in ALLOWED_SCOPES)
    cameras = None if row.camera_ids is None else frozenset(int(c) for c in row.camera_ids)
    return TokenPrincipal(owner, row.id, row.name, scopes, cameras)


# ── the events WebSocket ──────────────────────────────────────────────────

#: Scope a token needs to mint an events-socket ticket, and to open it.
WS_TICKET_SCOPE = "cameras.view"

#: Scope a token needs to RECEIVE each event type on the events socket.
#: Types not listed never reach a token (deny by default): a new event type
#: stays invisible to integrations until someone decides who may see it.
TOKEN_EVENT_SCOPES: dict[str, str] = {
    "camera_status": "cameras.view",
    "tracks": "live.view",
    "inference_result": "live.view",
    "inference_error": "live.view",
    "camera_event": "recordings.view",
    "app_alert": "alerts.view",
    "live_state": "cameras.view",
    "media_ready": "recordings.view",
    "site_mode": "settings.view",
    # Further filtered per entity by its required_scope (routers/events.py).
    "entity_state": "cameras.view",
    "descriptors_changed": "cameras.view",
}


def token_event_types(principal: TokenPrincipal) -> frozenset[str]:
    """Event types this token may receive on the events socket."""
    return frozenset(t for t, perm in TOKEN_EVENT_SCOPES.items()
                     if token_has_permission(principal, perm))


def principal_for_ws(db: Session, token_id: int, ip: str) -> TokenPrincipal | None:
    """Re-check a ticket's token at the WebSocket handshake.

    The ticket is up to 30 s old: the token may have been revoked, the owner
    disabled or the scope removed since. Also applies ``allowed_cidrs`` to
    the address the socket really comes from. None means refuse.
    """
    from models import ApiToken, User

    row = db.query(ApiToken).filter(ApiToken.id == token_id).first()
    if row is None or not _row_live(row):
        return None
    owner = db.query(User).filter(User.id == row.owner_user_id).first()
    if owner is None or not owner.is_active:
        return None
    if not _ip_allowed(ip, row.allowed_cidrs):
        return None
    principal = _principal(owner, row)
    if not token_has_permission(principal, WS_TICKET_SCOPE):
        return None
    _touch_last_used(row.id, ip)
    return principal


# ── device firewall ───────────────────────────────────────────────────────

_VALID_CACHE_TTL_S = 15.0
#: sha256 -> expires_at (aware datetime or None) of every live token, and
#: when that table was loaded. The firewall consults it on every /api
#: request, so it must not cost a query per request: a client sending
#: random ``onvr_`` bearers would otherwise turn each request into a DB
#: round trip on the event loop. One query per TTL, whatever the traffic.
_live: dict[str, datetime | None] = {}
_live_loaded_at: float | None = None


def _reload_live() -> None:
    global _live_loaded_at
    from core.database import SessionLocal
    from models import ApiToken

    table: dict[str, datetime | None] = {}
    with SessionLocal() as s:
        for row in s.query(ApiToken).filter(ApiToken.revoked_at.is_(None)).all():
            if _row_live(row):
                table[row.token_hash] = _as_aware(row.expires_at)
    _live.clear()
    _live.update(table)
    _live_loaded_at = time.monotonic()


def is_valid_token_cached(plain: str | None) -> bool:
    """Whether *plain* is a live token, from the in-memory table above (the
    device firewall runs on every /api request). A token is a bound
    credential, so it passes the firewall on its own; it is still fully
    authorized per request."""
    if not looks_like_token(plain) or not _TOKEN_RE.fullmatch(plain or ""):
        return False
    now = time.monotonic()
    if _live_loaded_at is None or now - _live_loaded_at >= _VALID_CACHE_TTL_S:
        try:
            _reload_live()
        except Exception:  # noqa: BLE001 — fail closed
            return False
    digest = hash_token(plain)
    if digest not in _live:
        return False
    expires = _live[digest]
    return expires is None or expires > datetime.now(UTC)


def invalidate_caches() -> None:
    """Drop cached validity after a revoke or mint, so it takes effect at once."""
    global _live_loaded_at
    _live_loaded_at = None
