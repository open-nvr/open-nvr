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

#: What a site sends of each camera:
#:   always    main stream (and the sub stream when the camera has one)
#:   sub_only  only the sub stream, into the camera's main path: live view and
#:             recording at sub-stream quality, for a thin uplink
LIVE_POLICIES = ("always", "sub_only")


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


def enrol(db: Session, code: str | None, *, ip: str | None, version: str | None,
          hostname: str | None = None) -> tuple[Site, str, bool] | None:
    """Trade a one-time code for the site's credential. None for a code that
    is unknown, used or expired (the caller answers all of them alike).
    Returns (site, credential, replaced): ``replaced`` when the site already
    had an agent, whose running streams the caller then ends."""
    from sqlalchemy import update

    raw = normalise_code(code)
    if len(raw) != CODE_LEN:
        return None
    code_hash = _sha256(raw)
    site = db.query(Site).filter(Site.enrol_code_hash == code_hash).first()
    if site is None:
        return None
    expires = _aware(site.enrol_expires_at)
    # Single use whatever happens next, and atomically: of two agents racing
    # with one code, only the one whose UPDATE clears it goes on.
    used = db.execute(
        update(Site)
        .where(Site.id == site.id, Site.enrol_code_hash == code_hash)
        .values(enrol_code_hash=None, enrol_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    if used.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    db.refresh(site)
    if expires is None or expires < _now():
        return None
    token, replaced = issue_credential(db, site, ip=ip, version=version, hostname=hostname)
    return site, token, replaced


def enrol_with_link(db: Session, token: str | None, *, ip: str | None, version: str | None,
                    hostname: str | None) -> tuple[Site, str, bool] | None:
    """Trade a setup link's ``<id>.<secret>`` for the site's credential
    (services/site_setup.py). Same answer as enrol()."""
    from services import site_setup

    site = site_setup.consume(db, token, ip=ip, hostname=hostname)
    if site is None:
        return None
    cred, replaced = issue_credential(db, site, ip=ip, version=version, hostname=hostname)
    return site, cred, replaced


def issue_credential(db: Session, site: Site, *, ip: str | None, version: str | None,
                     hostname: str | None) -> tuple[str, bool]:
    """A new credential for the site's agent (the old one stops working).
    Returns (credential, replaced).

    Replacing the agent of a site that already has cameras needs an admin's
    approval before the new agent is sent any camera (and so any camera
    password): a leaked code or link must not hand them over by itself."""
    replaced = bool(site.credential_hash)
    secret = secrets.token_urlsafe(32)
    site.credential_hash = _sha256(secret)
    site.credential_prefix = secret[:8]
    site.revoked_at = None
    site.enrolled_at = _now()
    site.last_ip = (ip or "")[:45] or None
    site.agent_version = (version or "")[:100] or None
    if replaced and site_cameras(db, site.id):
        site.approval_pending_at = _now()
        site.pending_agent = json.dumps({
            "hostname": (hostname or "")[:200] or None, "ip": site.last_ip,
            "version": site.agent_version, "credential_prefix": site.credential_prefix,
        })
    else:
        site.approval_pending_at = None
        site.pending_agent = None
    db.commit()
    notify(site.id)
    return f"{TOKEN_PREFIX}{site.id}_{secret}", replaced


def approve_agent(db: Session, site: Site) -> bool:
    """Let a replaced agent have the site's cameras. False if none waits."""
    if not site.approval_pending_at or not site.credential_hash:
        return False
    site.approval_pending_at = None
    site.pending_agent = None
    db.commit()
    notify(site.id)
    return True


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
    from models import SiteSetupToken

    site.credential_hash = None
    site.credential_prefix = None
    site.enrol_code_hash = None
    site.enrol_expires_at = None
    site.revoked_at = _now()
    site.approval_pending_at = None
    site.pending_agent = None
    # Unused setup links go too: revoking means nobody gets in by them.
    for link in db.query(SiteSetupToken).filter(SiteSetupToken.site_id == site.id,
                                                SiteSetupToken.used_at.is_(None),
                                                SiteSetupToken.revoked_at.is_(None)):
        link.revoked_at = _now()
    db.commit()
    notify(site.id)
    return site_paths(db, site)


def site_paths(db: Session, site: Site) -> list[str]:
    """The MediaMTX paths of the site's cameras (main and sub): whose running
    sessions end when the agent is cut off or replaced. A publish token is
    checked only when a session starts."""
    paths: list[str] = []
    for cam in site_cameras(db, site.id):
        main, sub = stream_names(cam)
        paths += [main, sub]
    return paths


def repin_proof(credential_hash: str | None, nonce: str, pin: str | None) -> str:
    """The server's proof that it is the server the agent enrolled with, for
    an agent that meets a new certificate key: HMAC-SHA256 keyed by the
    site's credential hash over the nonce and THIS server's key. A man in the
    middle shows the agent its own key and cannot make it. An unknown or
    revoked site, or no readable certificate, gets random bytes of the same
    shape, so the answer tells nothing. Mirrors host-agent site.RepinProof."""
    if not credential_hash or not pin:
        return secrets.token_hex(32)
    msg = "\n".join(("opennvr-site-repin", nonce, pin)).encode()
    return hmac.new(credential_hash.encode(), msg, hashlib.sha256).hexdigest()


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


def validate_lan_host(host: str | None) -> str | None:
    """An IP address or a plain host name on the site's LAN, or None."""
    import ipaddress
    import re as _re

    h = (host or "").strip().strip("[]")
    if not h:
        return None
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    if len(h) <= 253 and _re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*", h):
        return h.lower()
    raise ValueError("control_host must be an IP address or a host name")


def link_camera(
    db: Session,
    site: Site,
    camera: Camera,
    *,
    main_url: str,
    sub_url: str | None,
    username: str | None,
    password: str | None,
    control_host: str | None = None,
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
    # Where its control API (ONVIF, PTZ, settings) answers: the stream's host,
    # unless the camera's video comes from another box (an NVR channel, an
    # encoder) and the admin named it.
    camera.site_lan_host = validate_lan_host(control_host) or urlsplit(main).hostname
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
    if site.approval_pending_at:
        return out  # a replaced agent the admin has not approved yet
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
        main_src, sub_src = cam.site_main_url, cam.site_sub_url
        # sub_only: the camera's small stream goes into its main path (what
        # live view and recording read), and nothing else crosses the uplink.
        sub_only = site.live_policy == "sub_only" and bool(cam.site_sub_url)
        if sub_only:
            main_src, sub_src = cam.site_sub_url, None
        out.append({
            "camera_id": cam.id,
            "name": cam.name,
            "stream_name": main,
            "sub_stream_name": sub if sub_src else None,
            "main_url": _with_credentials(main_src, cam.username, password),
            "sub_url": _with_credentials(sub_src, cam.username, password),
            "main_from_sub": sub_only,
            "mode": "copy",
            # Where core's camera requests (ONVIF, PTZ) may go at this site.
            "control_host": cam.site_lan_host,
        })
    return out


def etag(desired: list[dict[str, Any]], site: Site) -> str:
    """Changes whenever anything the agent acts on changes."""
    blob = json.dumps(
        {"d": desired, "p": site.live_policy, "c": site.uplink_cap_kbps, "s": scan_request(site.id),
         "u": uplink_test_request(site.id)},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


# ── camera scan (the agent's scan.go) ───────────────────────────────────

#: The latest scan an admin asked for, per site (milliseconds; in memory:
#: a restart just forgets a request the agent had not picked up).
_scan_requests: dict[int, int] = {}


def request_scan(site_id: int) -> int:
    """Ask the site's agent for a camera scan; it starts on its next poll."""
    import time

    rid = int(time.time() * 1000)
    _scan_requests[site_id] = rid
    notify(site_id)
    return rid


def scan_request(site_id: int) -> int | None:
    return _scan_requests.get(site_id)


# ── uplink test (the agent's speedtest) ──────────────────────────────────

#: An uplink test an admin asked for, per site, and when: the agent then
#: uploads throwaway bytes to POST /site-agent/speedtest for a few seconds.
_uplink_requests: dict[int, int] = {}
#: At most one test per site this often: it costs the site's upload.
UPLINK_TEST_EVERY_S = 3600
#: The most a test uploads, and for how long (the agent stops at either).
UPLINK_TEST_MAX_BYTES = 10 << 20
UPLINK_TEST_SECONDS = 5


def request_uplink_test(site_id: int) -> int | None:
    """Ask the site's agent for an uplink test; None if one was asked for
    within UPLINK_TEST_EVERY_S."""
    import time

    now_ms = int(time.time() * 1000)
    last = _uplink_requests.get(site_id)
    if last and now_ms - last < UPLINK_TEST_EVERY_S * 1000:
        return None
    _uplink_requests[site_id] = now_ms
    notify(site_id)
    return now_ms


def uplink_test_request(site_id: int) -> int | None:
    return _uplink_requests.get(site_id)


#: Tests already run (one upload per request).
_uplink_done: dict[int, int] = {}


def uplink_test_wanted(site_id: int, test_id: int) -> bool:
    """The agent may upload only for the test an admin asked for, once:
    asking marks it used."""
    if not test_id or _uplink_requests.get(site_id) != test_id or _uplink_done.get(site_id) == test_id:
        return False
    _uplink_done[site_id] = test_id
    return True


def record_uplink_test(db: Session, site: Site, test_id: int, nbytes: int, seconds: float) -> dict[str, Any]:
    """Keep the result (in the report, beside what the agent reports)."""
    kbps = (nbytes * 8 / 1000) / seconds if seconds > 0 else 0.0
    result = {"id": test_id, "kbps": round(kbps, 1), "bytes": nbytes, "seconds": round(seconds, 2),
              "at": _now().isoformat()}
    try:
        report = json.loads(site.report) if site.report else {}
    except ValueError:
        report = {}
    report["uplink_test"] = result
    site.report = json.dumps(report, separators=(",", ":"), default=str)[:200_000]
    db.commit()
    return result


def set_camera_credentials(db: Session, site: Site, camera_ids: list[int] | None,
                           username: str | None, password: str | None) -> list[int]:
    """New user/password for the site's cameras (all, or those listed); the
    agent restarts their streams with them on its next poll."""
    from utils.encryption import encrypt_value

    changed = []
    for cam in site_cameras(db, site.id):
        if camera_ids is not None and cam.id not in camera_ids:
            continue
        if username is not None:
            cam.username = username.strip()[:50] or None
        if password is not None:
            cam.encrypted_password = encrypt_value(password) if password else None
        changed.append(cam.id)
    db.commit()
    notify(site.id)
    return changed


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
    try:
        before = json.loads(site.report) if site.report else {}
    except ValueError:
        before = {}
    if before.get("uplink_test") and "uplink_test" not in report:
        report = {**report, "uplink_test": before["uplink_test"]}
    site.last_seen_at = _now()
    site.last_ip = (ip or "")[:45] or None
    site.agent_version = str(report.get("version") or "")[:100] or None
    site.report = json.dumps(report, separators=(",", ":"), default=str)[:200_000]
    db.commit()


#: nginx's certificate as core sees it (compose mounts ./nginx-certs read-only).
NGINX_CERT_PATH_DEFAULT = "/app/nginx-certs/server.crt"
_pin_cache: tuple[float, str | None] = (-1.0, None)


def server_pin() -> str | None:
    """The SPKI pin of the certificate the site agent will meet on 443
    (``sha256/<base64>``, as `opennvr-host enrol -pin` takes it), or None when
    core cannot read it. Re-read when the file changes (Let's Encrypt)."""
    import base64
    import os

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    global _pin_cache
    path = os.environ.get("NGINX_CERT_PATH") or NGINX_CERT_PATH_DEFAULT
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    if mtime == _pin_cache[0]:
        return _pin_cache[1]
    pin: str | None = None
    try:
        with open(path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        spki = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        pin = "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode()
    except Exception:  # noqa: BLE001 - an unreadable cert just means no pin
        pin = None
    _pin_cache = (mtime, pin)
    return pin


def ingest_target(request_host: str | None) -> dict[str, Any]:
    """Where the agent sends video: this site's public name when set,
    otherwise the host the agent reached core on; always 443 with the
    ingest ALPN."""
    host = (getattr(settings, "public_hostname", "") or "").strip().rstrip(".").lower()
    if not host:
        host = (request_host or "").split(":")[0].strip("[]")
    return {"host": host, "port": 443, "alpn": INGEST_ALPN}


# ── the agent binary, served by this server ──────────────────────────────

#: Where the core image keeps the host agent builds (Dockerfile, stage 1b):
#: ``<os>-<arch>/opennvr-host[.exe]``, Windows also ``console/`` (see below).
AGENT_DIR_DEFAULT = "/opt/opennvr-host"
#: `uname -m` to this server's platform names (x86_64, aarch64, armv7l).
UNAME_SED = "'s/x86_64/amd64/;s/aarch64/arm64/;s/armv7l/arm/'"
AGENT_PLATFORMS = (*(f"{o}-{a}" for o in ("linux", "darwin", "windows") for a in ("amd64", "arm64")),
                   "linux-arm")  # 32-bit Raspberry Pi OS (ARMv7)
_sum_cache: dict[str, tuple[float, int, str]] = {}


def agent_binary(platform: str) -> tuple[str, str] | None:
    """(path, download file name) of the agent build for *platform*, or None.

    Windows gets the console build: the default one is a GUI-subsystem
    program (no window when the webcam agent starts at logon), and a GUI
    program run from a terminal prints nothing and is not waited for, so
    ``enrol`` would look like it did nothing."""
    import os

    if platform not in AGENT_PLATFORMS:
        return None
    root = os.environ.get("OPENNVR_HOST_AGENT_DIR") or AGENT_DIR_DEFAULT
    if platform.startswith("windows-"):
        candidates = [os.path.join(root, platform, "console", "opennvr-host.exe"),
                      os.path.join(root, platform, "opennvr-host.exe")]
        name = "opennvr-host.exe"
    else:
        candidates = [os.path.join(root, platform, "opennvr-host")]
        name = "opennvr-host"
    for path in candidates:
        if os.path.isfile(path):
            return path, name
    return None


def _sha256_file(path: str) -> str:
    """The file's SHA-256, cached by (mtime, size): the builds only change
    with the image, and hashing ~15 MB on every listing is waste."""
    import os

    st = os.stat(path)
    hit = _sum_cache.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    _sum_cache[path] = (st.st_mtime, st.st_size, h.hexdigest())
    return h.hexdigest()


def agent_build() -> str | None:
    import os

    root = os.environ.get("OPENNVR_HOST_AGENT_DIR") or AGENT_DIR_DEFAULT
    try:
        with open(os.path.join(root, "BUILD"), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def agent_update() -> tuple[bytes, bytes] | None:
    """The signed manifest of the agent builds this server ships, and its
    signature (manifest.json, manifest.sig, made by the release workflow;
    host-agent/internal/update). None for unsigned builds (a fork, a local
    build): site agents then do not update themselves. Relayed as they are:
    this server cannot sign one."""
    import os

    root = os.environ.get("OPENNVR_HOST_AGENT_DIR") or AGENT_DIR_DEFAULT
    try:
        with open(os.path.join(root, "manifest.json"), "rb") as fh:
            manifest = fh.read(1 << 20)
        with open(os.path.join(root, "manifest.sig"), "rb") as fh:
            sig = fh.read(1 << 16)
    except OSError:
        return None
    return (manifest, sig) if manifest and sig else None


def agent_downloads() -> list[dict[str, Any]]:
    """Every agent build this server has: platform, file name, size, sha256."""
    import os

    out = []
    for platform in AGENT_PLATFORMS:
        found = agent_binary(platform)
        if found:
            path, name = found
            out.append({"platform": platform, "filename": name,
                        "size": os.path.getsize(path), "sha256": _sha256_file(path)})
    return out


def setup_commands(host: str, code: str, pin: str | None) -> list[dict[str, str]]:
    """Copy-paste setup per OS, for the enrol dialog: fetch the agent from
    this server, enrol it, start it.

    The download first trusts the normal certificate chain (a Let's Encrypt
    certificate, or a front proxy's), and only if that fails the pinned key
    of this server's own certificate: the agent's enrol does the same. curl's
    ``--pinnedpubkey`` is checked even with ``-k``, so a self-signed server
    cannot be swapped on the way."""
    base = f"https://{host}/api/v1/site-agent/download"
    pin_arg = f"-pin {pin} " if pin else ""
    curl_pin = f"sha256//{pin.split('/', 1)[1]}" if pin and "/" in pin else None

    unix_url = f"{base}/$(uname -s | tr A-Z a-z)-$(uname -m | sed {UNAME_SED})"
    unix_dl = f'curl -fso opennvr-host "{unix_url}"'
    if curl_pin:
        unix_dl += f' || curl -fsSko opennvr-host --pinnedpubkey "{curl_pin}" "{unix_url}"'
    unix_dl = f"({unix_dl}) && chmod +x opennvr-host"

    win_url = f"{base}/windows-$($env:PROCESSOR_ARCHITECTURE.ToLower())"
    win_dl = f'curl.exe -fso opennvr-host.exe "{win_url}"'
    if curl_pin:
        win_dl += (f'; if ($LASTEXITCODE -ne 0) {{ curl.exe -fsSko opennvr-host.exe '
                   f'--pinnedpubkey "{curl_pin}" "{win_url}" }}')

    return [
        # Linux: installed as a service, so it starts again after a reboot
        # (macOS: `./opennvr-host site`, docs/SITE_AGENT.md).
        {"os": "linux", "label": "Linux / Raspberry Pi", "download": unix_dl,
         "enrol": f"./opennvr-host enrol {pin_arg}https://{host} {code}",
         "run": "sudo ./opennvr-host install-service"},
        {"os": "windows", "label": "Windows (PowerShell)", "download": win_dl,
         "enrol": f".\\opennvr-host.exe enrol {pin_arg}https://{host} {code}",
         "run": ".\\opennvr-host.exe site"},
    ]


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
        # Everything found, ONVIF discovery and the camera scan together,
        # one entry per address (the Add cameras dialog's list).
        "found": found_devices(report, linked_hosts),
        "scan": _scan_view(site.id, report),
        "uplink_test": _uplink_view(site.id, report),
        "live_policy": site.live_policy,
        "uplink_cap_kbps": site.uplink_cap_kbps,
        # What the agent's enrol checks the server's certificate against.
        "server_pin": server_pin(),
        # A replaced agent waiting for an admin's approval (no cameras yet).
        "approval_pending": bool(site.approval_pending_at),
        "pending_agent": _json_or_none(site.pending_agent),
    }


def found_devices(report: dict[str, Any], linked_hosts: set[str]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in ((report.get("onvif") or {}).get("devices") or []):
        ip = d.get("ip") if isinstance(d, dict) else None
        if isinstance(ip, str) and ip not in linked_hosts:
            out[ip] = {"ip": ip, "name": d.get("name"), "hardware": d.get("hardware"), "onvif": True,
                       "rtsp": False, "ports": []}
    for d in ((report.get("scan") or {}).get("devices") or []):
        ip = d.get("ip") if isinstance(d, dict) else None
        if not isinstance(ip, str) or ip in linked_hosts:
            continue
        entry = out.setdefault(ip, {"ip": ip, "name": None, "hardware": None, "onvif": False,
                                    "rtsp": False, "ports": []})
        entry["rtsp"] = bool(d.get("rtsp"))
        entry["ports"] = [p for p in (d.get("ports") or []) if isinstance(p, int)][:8]
    return sorted(out.values(), key=lambda e: tuple(int(x) if x.isdigit() else 0 for x in e["ip"].split(".")))


def _uplink_view(site_id: int, report: dict[str, Any]) -> dict[str, Any]:
    last = report.get("uplink_test") or {}
    asked = uplink_test_request(site_id)
    return {
        "requested": asked,
        "pending": bool(asked) and last.get("id") != asked,
        "kbps": last.get("kbps"),
        "at": last.get("at"),
    }


def _scan_view(site_id: int, report: dict[str, Any]) -> dict[str, Any]:
    scan = report.get("scan") or {}
    asked = scan_request(site_id)
    return {
        "requested": asked,
        # Asked for, and the agent has not answered that request yet.
        "pending": bool(asked) and scan.get("id") != asked,
        "scanned_at": scan.get("scanned_at"),
        "subnets": scan.get("subnets") or [],
        "note": scan.get("note"),
        "count": len(scan.get("devices") or []),
    }


def _json_or_none(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


# ── long polling ─────────────────────────────────────────────────────────

#: One event per site, set when its desired state may have changed. Core is
#: a single process (supervisord runs one uvicorn), so in-process is enough.
_events: dict[int, asyncio.Event] = {}


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
        try:
            owner.call_soon_threadsafe(ev.set)
        except RuntimeError:
            # The loop that waited is gone (a restart, a test client): no
            # one is waiting on this event any more.
            _events.pop(site_id, None)


#: Long polls held at once, all sites together. Each costs an open
#: connection and a coroutine (never a database connection: the route lets
#: go of its session before it waits). Over this, a poll is answered at once
#: and the agent polls every few seconds instead.
WAIT_GLOBAL_MAX = 2000
_holding = 0


def arm(site_id: int) -> asyncio.Event:
    """A fresh event for the site's next long poll. Arm BEFORE reading the
    state the agent compares against: a change between the read and the wait
    then still wakes it.

    A site holds at most one poll: arming wakes the one already held (the
    agent reconnected after a blip and the old request is still open, or a
    second agent uses this site's credential), which then answers at once."""
    old = _events.get(site_id)
    if old is not None and getattr(old, "_opennvr_held", False):
        old.set()
    ev = _events[site_id] = asyncio.Event()
    ev._opennvr_loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    return ev


def holding() -> int:
    """Long polls held right now (all sites)."""
    return _holding


async def wait(ev: asyncio.Event, timeout: float = WAIT_MAX_S) -> bool:
    """Wait on an armed event. True if woken, False on the timeout (or at
    once, when WAIT_GLOBAL_MAX polls are already held)."""
    global _holding
    if _holding >= WAIT_GLOBAL_MAX:
        return False
    _holding += 1
    ev._opennvr_held = True  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(ev.wait(), timeout)
        return True
    except TimeoutError:
        return False
    finally:
        ev._opennvr_held = False  # type: ignore[attr-defined]
        _holding -= 1
