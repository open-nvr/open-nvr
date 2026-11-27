# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Remote sites and their agents (issue #925, cloud ingest).

A cloud-hosted OpenNVR cannot dial cameras behind a customer's NAT, so a
small agent at the site (the host agent in site mode) dials out instead:
HTTPS on 443 for control, RTSP over TLS on 443 (ALPN opennvr-rtsps, ended at
nginx) for video. This module is core's side of that.

- Enrolment: an admin creates a site and gets a one-time code (10 base32
  characters, 15 minutes, single use, only its hash stored). The agent trades
  it for a long-lived credential ``site_<id>_<secret>``; again only the hash is
  kept. Re-enrolling replaces the credential; revoking clears it and ends the
  site's running streams.
- Desired state: the agent asks which cameras to relay. A site camera is a
  publisher camera with ``site_id`` set and the camera's RTSP URLs on the
  site's LAN; the credentials go to the agent inside those URLs, over TLS,
  and only to the agent of that site. Core never dials those addresses.
- Publish tokens: 5-minute, path-scoped MediaMTX tokens for that site's
  cameras only, asked for on every (re)connect.
- Long polling: the agent waits on ``wait`` and is woken when its desired
  state changes, so a camera added in the UI starts within a second.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from core.camera_source import SOURCE_PUBLISHER
from core.config import settings
from models import Camera, Site

ENROL_CODE_TTL = timedelta(minutes=15)
#: base32 without the look-alikes 0/O, 1/I/L: what a person types.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 10
TOKEN_PREFIX = "site_"
#: A site is "online" while it synced this recently (it syncs every few
#: seconds and long-polls in between).
ONLINE_WITHIN_S = 60
#: How long the agent's long poll is held.
WAIT_MAX_S = 25
#: The ALPN the 443 stream layer routes to camera ingest (nginx/stream.conf.template).
INGEST_ALPN = "opennvr-rtsps"

LIVE_POLICIES = ("always", "sub_only", "on_demand_main")


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands timezone-aware columns back naive; treat those as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ── enrolment codes ──────────────────────────────────────────────────────


def normalise_code(code: str | None) -> str:
    """What the person typed, as stored: upper case, no separators."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def format_code(code: str) -> str:
    return f"{code[:5]}-{code[5:]}"


def issue_enrol_code(db: Session, site: Site) -> tuple[str, datetime]:
    """A new one-time code for ``site`` (replacing any unused one). Returns
    the code as shown to the admin and when it expires."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LEN))
    site.enrol_code_hash = _sha256(raw)
    site.enrol_expires_at = _now() + ENROL_CODE_TTL
    db.commit()
    return format_code(raw), site.enrol_expires_at


def create_site(db: Session, name: str, user_id: int | None) -> tuple[Site, str, datetime]:
    site = Site(name=name.strip()[:100], created_by=user_id, live_policy="always")
    db.add(site)
    db.flush()
    code, expires = issue_enrol_code(db, site)
    db.refresh(site)
    return site, code, expires


def enrol(db: Session, code: str | None, *, ip: str | None, version: str | None) -> tuple[Site, str] | None:
    """Trade a one-time code for the site's credential. None for a code that
    is unknown, used or expired (the caller answers all of them alike)."""
    raw = normalise_code(code)
    if len(raw) != CODE_LEN:
        return None
    site = db.query(Site).filter(Site.enrol_code_hash == _sha256(raw)).first()
    if site is None:
        return None
    expires = _aware(site.enrol_expires_at)
    # Single use whatever happens next.
    site.enrol_code_hash = None
    site.enrol_expires_at = None
    if expires is None or expires < _now():
        db.commit()
        return None
    secret = secrets.token_urlsafe(32)
    site.credential_hash = _sha256(secret)
    site.credential_prefix = secret[:8]
    site.revoked_at = None
    site.enrolled_at = _now()
    site.last_ip = (ip or "")[:45] or None
    site.agent_version = (version or "")[:100] or None
    db.commit()
    notify(site.id)
    return site, f"{TOKEN_PREFIX}{site.id}_{secret}"


def authenticate(db: Session, token: str | None) -> Site | None:
    """The site a ``site_<id>_<secret>`` credential belongs to, or None.
    Constant-time on the secret; a revoked or never-enrolled site has no
    credential at all."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX):]
    site_id, sep, secret = rest.partition("_")
    if not sep or not site_id.isdigit() or not secret:
        return None
    site = db.get(Site, int(site_id))
    if site is None or not site.credential_hash or site.revoked_at is not None:
        return None
    if not hmac.compare_digest(site.credential_hash, _sha256(secret)):
        return None
    return site


def revoke(db: Session, site: Site) -> list[str]:
    """Clear the site's credential (and any unused code). Returns the
    MediaMTX paths of its cameras, whose running sessions the caller ends:
    a publish token is checked only when a session starts."""
    site.credential_hash = None
    site.credential_prefix = None
    site.enrol_code_hash = None
    site.enrol_expires_at = None
    site.revoked_at = _now()
    db.commit()
    notify(site.id)
    paths: list[str] = []
    for cam in site_cameras(db, site.id):
        main, sub = stream_names(cam)
        paths += [main, sub]
    return paths


# ── cameras ──────────────────────────────────────────────────────────────


def site_cameras(db: Session, site_id: int) -> list[Camera]:
    return (
        db.query(Camera)
        .filter(Camera.site_id == site_id, Camera.deleted_at.is_(None))
        .order_by(Camera.id)
        .all()
    )


def stream_names(camera: Camera) -> tuple[str, str]:
    from services.stream_service import _build_stream_name, substream_name

    main = _build_stream_name(settings.mediamtx_stream_prefix, camera.id, camera.ip_address)
    return main, substream_name(main)


def validate_lan_url(url: str | None, *, field: str) -> str | None:
    """A camera's RTSP URL on the site's LAN: rtsp/rtsps, a host, no
    credentials (those live in the camera's username/password)."""
    if url is None or not url.strip():
        return None
    u = url.strip()
    parts = urlsplit(u)
    if parts.scheme not in ("rtsp", "rtsps") or not parts.hostname:
        raise ValueError(f"{field} must be an rtsp:// or rtsps:// URL with a host")
    if parts.username or parts.password:
        raise ValueError(f"{field} must not carry credentials; set username and password instead")
    if len(u) > 500:
        raise ValueError(f"{field} is too long")
    return u


def link_camera(
    db: Session,
    site: Site,
    camera: Camera,
    *,
    main_url: str,
    sub_url: str | None,
    username: str | None,
    password: str | None,
) -> Camera:
    """Make ``camera`` (a publisher camera created the usual way) one of the
    site's: the agent there relays ``main_url`` (and ``sub_url``) into its
    paths. Raises ValueError for anything that does not fit."""
    from utils.encryption import encrypt_value

    if camera.source_type != SOURCE_PUBLISHER or camera.source_device:
        raise ValueError("only a pushed camera (no address of its own, no webcam) can belong to a site")
    if camera.site_id not in (None, site.id):
        raise ValueError("the camera belongs to another site")
    main = validate_lan_url(main_url, field="main_url")
    if not main:
        raise ValueError("main_url is required")
    sub = validate_lan_url(sub_url, field="sub_url")
    camera.site_id = site.id
    camera.site_main_url = main
    camera.site_sub_url = sub
    camera.site_lan_host = urlsplit(main).hostname
    if username is not None:
        camera.username = username.strip()[:50] or None
    if password is not None:
        camera.encrypted_password = encrypt_value(password) if password else None
    db.commit()
    notify(site.id)
    return camera


def unlink_camera(db: Session, camera: Camera) -> int | None:
    site_id = camera.site_id
    camera.site_id = None
    camera.site_main_url = None
    camera.site_sub_url = None
    camera.site_lan_host = None
    db.commit()
    if site_id:
        notify(site_id)
    return site_id


def _with_credentials(url: str | None, username: str | None, password: str | None) -> str | None:
    if not url:
        return None
    if not username:
        return url
    parts = urlsplit(url)
    auth = quote(username, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{auth}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def desired_state(db: Session, site: Site) -> list[dict[str, Any]]:
    """What the site's agent should relay: one entry per active camera,
    with the camera's LAN URLs (credentials inside, for that agent only)
    and the MediaMTX paths they go to."""
    from utils.encryption import decrypt_value

    out: list[dict[str, Any]] = []
    for cam in site_cameras(db, site.id):
        if not cam.is_active or not cam.site_main_url:
            continue
        password = None
        if cam.encrypted_password:
            try:
                password = decrypt_value(cam.encrypted_password)
            except Exception:  # noqa: BLE001 - a broken secret must not stop the others
                password = None
        main, sub = stream_names(cam)
        out.append({
            "camera_id": cam.id,
            "name": cam.name,
            "stream_name": main,
            "sub_stream_name": sub if cam.site_sub_url else None,
            "main_url": _with_credentials(cam.site_main_url, cam.username, password),
            "sub_url": _with_credentials(cam.site_sub_url, cam.username, password),
            "mode": "copy",
        })
    return out


def etag(desired: list[dict[str, Any]], site: Site) -> str:
    """Changes whenever anything the agent acts on changes."""
    blob = json.dumps(
        {"d": desired, "p": site.live_policy, "c": site.uplink_cap_kbps},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def camera_for_site(db: Session, site: Site, camera_id: int) -> Camera | None:
    cam = db.get(Camera, camera_id)
    if cam is None or cam.site_id != site.id or cam.deleted_at is not None or not cam.is_active:
        return None
    return cam


def publish_token(camera: Camera) -> dict[str, Any]:
    """A 5-minute token for the camera's main path (and its -sub when the
    site relays one), and nothing else."""
    from core.camera_source import PUBLISH_TOKEN_MINUTES
    from services.mediamtx_jwt_service import MediaMtxJwtService

    main, sub = stream_names(camera)
    kwargs: dict[str, Any] = {}
    if camera.site_sub_url:
        kwargs["extra_paths"] = [sub]
    token = MediaMtxJwtService.create_publish_token(
        camera_id=camera.id,
        camera_path=main,
        expiry_minutes=PUBLISH_TOKEN_MINUTES,
        **kwargs,
    )
    return {"camera_id": camera.id, "stream_name": main,
            "sub_stream_name": sub if camera.site_sub_url else None,
            "token": token, "expires_in_seconds": PUBLISH_TOKEN_MINUTES * 60}


# ── reports ──────────────────────────────────────────────────────────────


def record_report(db: Session, site: Site, report: dict[str, Any], ip: str | None) -> None:
    site.last_seen_at = _now()
    site.last_ip = (ip or "")[:45] or None
    site.agent_version = str(report.get("version") or "")[:100] or None
    site.report = json.dumps(report, separators=(",", ":"), default=str)[:200_000]
    db.commit()


def ingest_target(request_host: str | None) -> dict[str, Any]:
    """Where the agent sends video: this site's public name when set,
    otherwise the host the agent reached core on; always 443 with the
    ingest ALPN."""
    host = (getattr(settings, "public_hostname", "") or "").strip().rstrip(".").lower()
    if not host:
        host = (request_host or "").split(":")[0].strip("[]")
    return {"host": host, "port": 443, "alpn": INGEST_ALPN}


def public_view(db: Session, site: Site) -> dict[str, Any]:
    last = _aware(site.last_seen_at)
    try:
        report = json.loads(site.report) if site.report else {}
    except ValueError:
        report = {}
    cams = site_cameras(db, site.id)
    onvif = (report.get("onvif") or {}).get("devices") or []
    linked_hosts = {c.site_lan_host for c in cams if c.site_lan_host}
    return {
        "id": site.id,
        "name": site.name,
        "created_at": site.created_at,
        "enrolled": bool(site.credential_hash),
        "enrolled_at": site.enrolled_at,
        "revoked_at": site.revoked_at,
        "credential_prefix": site.credential_prefix,
        "code_pending": bool(site.enrol_code_hash),
        "code_expires_at": site.enrol_expires_at,
        "online": bool(last and (_now() - last).total_seconds() < ONLINE_WITHIN_S),
        "last_seen_at": site.last_seen_at,
        "last_ip": site.last_ip,
        "agent_version": site.agent_version,
        "hostname": report.get("hostname"),
        "publishers": report.get("publishers") or [],
        "cameras": [
            {"id": c.id, "name": c.name, "lan_host": c.site_lan_host,
             "main_url": c.site_main_url, "sub_url": c.site_sub_url, "is_active": c.is_active}
            for c in cams
        ],
        # ONVIF devices the agent found on the site's LAN, not yet linked.
        "discovered": [d for d in onvif if d.get("ip") not in linked_hosts],
        "live_policy": site.live_policy,
        "uplink_cap_kbps": site.uplink_cap_kbps,
    }


# ── long polling ─────────────────────────────────────────────────────────

#: One event per site, set when its desired state may have changed. Core is
#: a single process (supervisord runs one uvicorn), so in-process is enough.
_events: dict[int, asyncio.Event] = {}


def _event(site_id: int) -> asyncio.Event:
    ev = _events.get(site_id)
    if ev is None:
        ev = _events[site_id] = asyncio.Event()
    return ev


def notify(site_id: int) -> None:
    """Wake the site's waiting agent (from any thread)."""
    ev = _events.get(site_id)
    if ev is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        ev.set()
        return
    # Called from a worker thread (a sync route): hand it to the loop the
    # waiter is on.
    owner = getattr(ev, "_opennvr_loop", None)
    if owner is not None:
        owner.call_soon_threadsafe(ev.set)


def arm(site_id: int) -> asyncio.Event:
    """Clear and return the site's event. Arm BEFORE reading the state the
    agent compares against: a change between the read and the wait then
    still wakes it."""
    ev = _event(site_id)
    ev._opennvr_loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    ev.clear()
    return ev


async def wait(ev: asyncio.Event, timeout: float = WAIT_MAX_S) -> bool:
    """Wait on an armed event. True if woken, False on the timeout."""
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except TimeoutError:
        return False
