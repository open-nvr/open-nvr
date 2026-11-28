# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Remote sites (services/sites.py, issue #925 cloud ingest).

``/sites`` is the admin side, behind ``cameras.manage``: create a site and
its one-time enrolment code, link pushed cameras to it, revoke it.

``/site-agent`` is the agent's side. Unlike the host agent's routes it is
meant to be reached THROUGH nginx (the agent is at another site, on the
internet), so it does not refuse proxied requests; it authenticates with
``Authorization: Bearer site_<id>_<secret>`` instead. Every failure there is
the same 404, so a caller learns nothing about which part was wrong.
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from core.client_ip import get_client_ip
from core.database import get_db
from core.permissions import RequirePermission
from models import Camera, Site, User
from services import host_agent
from services import site_health, site_setup
from services import sites as svc

router = APIRouter(prefix="/sites", tags=["sites"])
agent_router = APIRouter(prefix="/site-agent", tags=["site-agent"])
_require_cameras_manage = RequirePermission("cameras.manage")


# ── rate limits ────────────────────────────────────────────────────────

_calls: dict[str, list[float]] = {}


def _rate_limited(key: str, per_minute: int) -> bool:
    now = time.monotonic()
    calls = [t for t in _calls.get(key, []) if now - t < 60]
    limited = len(calls) >= per_minute
    if not limited:
        calls.append(now)
    _calls[key] = calls
    if len(_calls) > 4096:  # never grow without bound
        for k in [k for k, v in _calls.items() if not v or now - v[-1] >= 60]:
            _calls.pop(k, None)
    return limited


#: Enrolment tries per client address per minute: a code is 10 characters
#: from a 31-letter alphabet and lives 15 minutes, so guessing is hopeless,
#: and this keeps it that way.
ENROL_PER_MINUTE = 5
#: ...and from all addresses together, so many addresses (or a front proxy
#: that hides them all behind one) cannot make guessing faster.
ENROL_GLOBAL_PER_MINUTE = 60
#: Key proofs (repin) per client address per minute: an agent asks once
#: when the server's certificate key changes.
REPIN_PER_MINUTE = 10
#: Setup-link lookups (the public page, and the agent's hello) per client
#: address per minute. A link's id is 48 random bits: not guessable, and
#: unknown ids get answers that look like real ones.
SETUP_PER_MINUTE = 30
#: Agent calls per site per minute, per kind, so one kind cannot starve the
#: others: control (a sync every few seconds and a long poll), publish
#: tokens (one per stream (re)connect, main + sub of every camera), and the
#: answers to camera requests (a camera's ONVIF probe is several each).
AGENT_PER_MINUTE = 240
TOKENS_PER_MINUTE = 240
RPC_RESULTS_PER_MINUTE = 1200


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


# ── admin ──────────────────────────────────────────────────────────────


class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class SiteUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    live_policy: Literal["always", "sub_only"] | None = None
    uplink_cap_kbps: int | None = Field(None, ge=0, le=10_000_000)
    alert_offline: bool | None = None
    alert_camera_down: bool | None = None
    agent_updates: bool | None = None


class CameraLink(BaseModel):
    main_url: str = Field(..., min_length=8, max_length=500)
    sub_url: str | None = Field(None, max_length=500)
    username: str | None = Field(None, max_length=50)
    password: str | None = Field(None, max_length=200)
    # Where the camera's control API answers, when it is not the stream's host.
    control_host: str | None = Field(None, max_length=255)


def _site_or_404(db: Session, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found")
    return site


def _enrol_host(request: Request) -> str:
    return svc.ingest_target(request.headers.get("host"))["host"] or "<this-server>"


def _enrol_command(request: Request, code: str) -> str:
    pin = svc.server_pin()
    # The pin lets a self-signed server be trusted from the first call;
    # with a Let's Encrypt certificate it is harmless (the chain verifies).
    pin_arg = f"-pin {pin} " if pin else ""
    return f"opennvr-host enrol {pin_arg}https://{_enrol_host(request)} {code}"


def _enrol_answer(request: Request, code: str, expires) -> dict:
    return {"enrol_code": code, "enrol_expires_at": expires,
            "enrol_command": _enrol_command(request, code),
            "setup": svc.setup_commands(_enrol_host(request), code, svc.server_pin())}


@router.get("")
def list_sites(db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    return [svc.public_view(db, s) for s in db.query(Site).order_by(Site.id).all()]


@router.post("", status_code=201)
def create_site(
    body: SiteCreate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """A new site and its one-time enrolment code (shown once)."""
    from services.audit_service import audit_request

    site, code, expires = svc.create_site(db, body.name, user.id)
    audit_request(db, request, action="site.create", user_id=user.id,
                  entity_type="site", entity_id=site.id, details={"name": site.name})
    return {**svc.public_view(db, site), **_enrol_answer(request, code, expires)}


@router.get("/{site_id}")
def get_site(site_id: int, db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    return svc.public_view(db, _site_or_404(db, site_id))


@router.patch("/{site_id}")
def update_site(
    site_id: int,
    body: SiteUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    site = _site_or_404(db, site_id)
    if body.name is not None:
        site.name = body.name.strip()
    if body.live_policy is not None:
        site.live_policy = body.live_policy
    if "uplink_cap_kbps" in body.model_fields_set:
        site.uplink_cap_kbps = body.uplink_cap_kbps or None
    for flag in ("alert_offline", "alert_camera_down", "agent_updates"):
        value = getattr(body, flag)
        if value is not None:
            setattr(site, flag, value)
    db.commit()
    svc.notify(site.id)
    return svc.public_view(db, site)


@router.post("/{site_id}/enrol-code")
def new_enrol_code(
    site_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """A fresh one-time code: to (re-)enrol the site's agent. The current
    credential keeps working until the code is used."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    code, expires = svc.issue_enrol_code(db, site)
    audit_request(db, request, action="site.enrol_code", user_id=user.id,
                  entity_type="site", entity_id=site.id)
    return _enrol_answer(request, code, expires)


class SetupLinkCreate(BaseModel):
    hours: int = Field(site_setup.LINK_HOURS_DEFAULT, ge=site_setup.LINK_HOURS_MIN, le=site_setup.LINK_HOURS_MAX)


@router.get("/setup-links/status")
def setup_link_status(refresh: bool = Query(False), user: User = Depends(_require_cameras_manage)):
    """Whether this server can hand out setup links, and if not why
    (services/site_setup.py readiness)."""
    return site_setup.readiness(force=refresh)


@router.post("/{site_id}/setup-links", status_code=201)
def create_setup_link(
    site_id: int,
    request: Request,
    body: SetupLinkCreate | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """A setup link for the site (shown once: its secret is only in the
    returned URL). 409 when this server cannot offer links; the site then
    uses its enrolment code."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    ready = site_setup.readiness()
    if not ready["ready"]:
        raise HTTPException(status_code=409, detail={"reason": ready["reason"], "message": ready["detail"]})
    link = site_setup.create_link(db, site, user_id=user.id, hours=(body or SetupLinkCreate()).hours,
                                  host=ready["host"])
    audit_request(db, request, action="site.setup_link", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"token_id": link["token_id"], "expires_at": str(link["expires_at"])})
    return link


@router.get("/{site_id}/setup-links")
def list_setup_links(site_id: int, db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    return site_setup.list_links(db, _site_or_404(db, site_id))


@router.delete("/{site_id}/setup-links/{token_id}", status_code=204)
def revoke_setup_link(
    site_id: int,
    token_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if not site_setup.revoke_link(db, site, token_id[:16]):
        raise HTTPException(status_code=404, detail="No usable link with that id")
    audit_request(db, request, action="site.setup_link_revoke", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"token_id": token_id[:16]})


@router.post("/{site_id}/approve-agent")
def approve_agent(
    site_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """Let a replaced agent (re-enrolled while the site had cameras) have
    the site's cameras and their passwords."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    pending = site.pending_agent
    if not svc.approve_agent(db, site):
        raise HTTPException(status_code=409, detail="No agent is waiting for approval")
    audit_request(db, request, action="site.agent_approved", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"agent": pending})
    return svc.public_view(db, site)


@router.post("/{site_id}/scan")
def scan_site(site_id: int, request: Request, db: Session = Depends(get_db),
              user: User = Depends(_require_cameras_manage)):
    """Ask the site's agent to look for cameras on its networks (camera
    ports only; services/site_probe.py, the agent's scan.go). The result
    arrives with its next report, in ``found``."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if not site.credential_hash:
        raise HTTPException(status_code=409, detail="The site has no agent yet")
    if _rate_limited(f"scan:{site.id}", 6):
        raise HTTPException(status_code=429, detail="A scan was just asked for; wait a minute")
    rid = svc.request_scan(site.id)
    audit_request(db, request, action="site.camera_scan", user_id=user.id, entity_type="site", entity_id=site.id)
    return {"requested": rid}


@router.post("/{site_id}/checks")
def run_site_checks(site_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(_require_cameras_manage)):
    """Ask the site's agent to test, from the site: this server, the video
    route and each of its cameras. The result arrives in ``checks``."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if not site.credential_hash:
        raise HTTPException(status_code=409, detail="The site has no agent yet")
    if _rate_limited(f"checks:{site.id}", 6):
        raise HTTPException(status_code=429, detail="Checks were just asked for; wait a minute")
    rid = site_health.request_checks(site.id)
    audit_request(db, request, action="site.checks", user_id=user.id, entity_type="site", entity_id=site.id)
    return {"requested": rid}


@router.post("/{site_id}/uplink-test")
def uplink_test(site_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(_require_cameras_manage)):
    """Ask the site's agent to measure its free upload: it uploads throwaway
    bytes for a few seconds (at most 10 MB, at most once an hour). The
    result arrives in ``uplink_test`` on the site."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if not site.credential_hash:
        raise HTTPException(status_code=409, detail="The site has no agent yet")
    rid = svc.request_uplink_test(site.id)
    if rid is None:
        raise HTTPException(status_code=429, detail="The uplink was tested less than an hour ago")
    audit_request(db, request, action="site.uplink_test", user_id=user.id, entity_type="site", entity_id=site.id)
    return {"requested": rid}


class CameraCredentials(BaseModel):
    camera_ids: list[int] | None = Field(None, max_length=512)
    username: str | None = Field(None, max_length=50)
    password: str | None = Field(None, max_length=200)


@router.post("/{site_id}/cameras/credentials")
def set_site_camera_credentials(site_id: int, body: CameraCredentials, request: Request,
                                db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    """A new user and password for the site's cameras (all of them, or those
    listed): after the cameras' own password was changed, say."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if body.username is None and body.password is None:
        raise HTTPException(status_code=422, detail="Give a user, a password, or both")
    changed = svc.set_camera_credentials(db, site, body.camera_ids, body.username, body.password)
    audit_request(db, request, action="site.camera_credentials", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"cameras": changed, "username_changed": body.username is not None,
                                               "password_changed": body.password is not None})
    return {**svc.public_view(db, site), "changed": changed}


class ProbeRequest(BaseModel):
    ips: list[str] = Field(..., min_length=1, max_length=64)
    username: str = Field("", max_length=50)
    password: str = Field("", max_length=200)


@router.post("/{site_id}/probe")
async def probe_site_devices(site_id: int, body: ProbeRequest, request: Request,
                             db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    """Try the ticked devices with one user and password, through the
    site's agent: their channels, streams and a preview (services/site_probe.py).
    Only devices the agent reported; nothing is saved."""
    import ipaddress

    from core.database import release
    from services import site_probe, site_relay
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    site_key = site.id
    if not site.credential_hash:
        raise HTTPException(status_code=409, detail="The site has no agent yet")
    ips = []
    for ip in dict.fromkeys(body.ips):
        try:
            ips.append(str(ipaddress.IPv4Address(ip)))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{ip!r} is not an IPv4 address") from None
    known = site_relay.reported_devices(site_key)
    unknown = [ip for ip in ips if ip not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not found at this site: {', '.join(unknown)}")
    if _rate_limited(f"probe:{site_key}", 10):
        raise HTTPException(status_code=429, detail="Too many probes; wait a minute")
    # The password goes to these devices (through the agent): on the record.
    audit_request(db, request, action="site.camera_probe", user_id=user.id, entity_type="site",
                  entity_id=site_key, details={"devices": ips, "count": len(ips),
                                               "username": body.username or None})
    release(db)  # probing takes seconds per device: no pooled connection meanwhile
    results = await site_probe.probe_devices(site_key, ips, body.username, body.password)
    return {"results": results}


class BulkCamera(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    main_url: str = Field(..., min_length=8, max_length=500)
    sub_url: str | None = Field(None, max_length=500)
    control_host: str | None = Field(None, max_length=255)


class BulkAdd(BaseModel):
    cameras: list[BulkCamera] = Field(..., min_length=1, max_length=64)
    username: str | None = Field(None, max_length=50)
    password: str | None = Field(None, max_length=200)


@router.post("/{site_id}/cameras/bulk", status_code=201)
async def bulk_add_cameras(site_id: int, body: BulkAdd, request: Request,
                           db: Session = Depends(get_db), user: User = Depends(_require_cameras_manage)):
    """Add several of the site's cameras at once (what a probe found, or
    typed in). Each is created like any camera (POST /cameras/) and linked to
    the site; MediaMTX paths are set up one camera after another, not all at
    once. All or nothing on a bad URL: nothing is created then."""
    from routers.cameras import create_camera_checked
    from schemas import CameraCreate
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    for i, c in enumerate(body.cameras):
        try:
            svc.validate_lan_url(c.main_url, field=f"cameras[{i}].main_url")
            svc.validate_lan_url(c.sub_url, field=f"cameras[{i}].sub_url")
            svc.validate_lan_host(c.control_host)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    made: list[int] = []
    for c in body.cameras:
        cam = await create_camera_checked(
            CameraCreate(name=c.name.strip(), source_type="publisher"),
            force=True, db=db, current_user=user, request=request, probe=False, duplicate_check=False,
        )
        row = db.get(Camera, cam.id)
        svc.link_camera(db, site, row, main_url=c.main_url, sub_url=c.sub_url,
                        username=body.username, password=body.password, control_host=c.control_host)
        made.append(cam.id)
    audit_request(db, request, action="site.cameras_bulk_add", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"cameras": made})
    view = svc.public_view(db, site)
    for camera_id in made:  # one at a time: never a burst of MediaMTX changes
        await _reprovision(camera_id)
    return {**view, "added": made}


@router.delete("/{site_id}/credential")
async def revoke_site(
    site_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """Cut the site off: its agent's credential stops working at once and
    its running streams are ended. Its cameras and recordings stay."""
    from services.audit_service import audit_request
    from services.mediamtx_admin_service import MediaMtxAdminService

    site = _site_or_404(db, site_id)
    paths = svc.revoke(db, site)
    kicked = await MediaMtxAdminService.kick_path_sessions(paths)
    audit_request(db, request, action="site.revoke", user_id=user.id, entity_type="site",
                  entity_id=site.id, details={"kicked": len(kicked.get("kicked") or []),
                                               "kick_errors": kicked.get("errors") or []})
    return {**svc.public_view(db, site), "kicked": kicked}


@router.delete("/{site_id}", status_code=204)
def delete_site(
    site_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """Remove a site that has no cameras left (unlink or delete them first)."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    if svc.site_cameras(db, site.id):
        raise HTTPException(status_code=409, detail="The site still has cameras; unlink or delete them first")
    db.delete(site)
    db.commit()
    svc.notify(site_id)
    audit_request(db, request, action="site.delete", user_id=user.id, entity_type="site", entity_id=site_id)


async def _reprovision(camera_id: int) -> None:
    """Re-provision a camera's MediaMTX paths after its site link changed:
    a sub stream needs its ``-sub`` path (local_webcams.provisioning_for)."""
    from services.mediamtx_startup_service import MediaMtxStartupService

    try:
        await MediaMtxStartupService.provision_camera_by_id(camera_id, force=True)
    except Exception:  # noqa: BLE001 - the startup reconcile provisions it again
        pass


@router.put("/{site_id}/cameras/{camera_id}")
async def link_camera(
    site_id: int,
    camera_id: int,
    body: CameraLink,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    """Make a pushed camera (Add Camera, source "pushed") one of the site's:
    its agent relays ``main_url`` (and ``sub_url``) from the site's LAN.
    Credentials go in username/password, never in the URLs."""
    from services.audit_service import audit_request

    site = _site_or_404(db, site_id)
    cam = db.get(Camera, camera_id)
    if cam is None or cam.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Camera not found")
    try:
        svc.link_camera(db, site, cam, main_url=body.main_url, sub_url=body.sub_url,
                        username=body.username, password=body.password,
                        control_host=body.control_host)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    audit_request(db, request, action="site.camera_link", user_id=user.id, entity_type="camera",
                  entity_id=cam.id, details={"site_id": site.id, "lan_host": cam.site_lan_host})
    view = svc.public_view(db, site)
    await _reprovision(cam.id)
    return view


@router.delete("/{site_id}/cameras/{camera_id}")
async def unlink_camera(
    site_id: int,
    camera_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(_require_cameras_manage),
):
    from services.audit_service import audit_request
    from services.mediamtx_admin_service import MediaMtxAdminService

    site = _site_or_404(db, site_id)
    cam = db.get(Camera, camera_id)
    if cam is None or cam.site_id != site.id:
        raise HTTPException(status_code=404, detail="Camera not found on this site")
    paths = list(svc.stream_names(cam))
    svc.unlink_camera(db, cam)
    await MediaMtxAdminService.kick_path_sessions(paths)
    await _reprovision(cam.id)
    audit_request(db, request, action="site.camera_unlink", user_id=user.id, entity_type="camera",
                  entity_id=cam.id, details={"site_id": site.id})
    return svc.public_view(db, site)


# ── agent ──────────────────────────────────────────────────────────────


class EnrolRequest(BaseModel):
    # Either the short code (typed) or a setup link's "<id>.<secret>".
    code: str | None = Field(None, min_length=4, max_length=32)
    token: str | None = Field(None, min_length=20, max_length=90)
    version: str | None = Field(None, max_length=100)
    hostname: str | None = Field(None, max_length=200)

    @field_validator("token")
    @classmethod
    def _one_of(cls, v, info):
        if v is not None and info.data.get("code") is not None:
            raise ValueError("send a code or a token, not both")
        return v


class PublisherState(BaseModel):
    camera_id: int
    stream: Literal["main", "sub"] = "main"
    state: Literal["starting", "live", "error", "waiting", "uplink_cap"]
    detail: str | None = Field(None, max_length=300)
    since: float | None = None
    kbps: float | None = None
    # Which relay carries it at the site: the agent's own, or ffmpeg (the
    # fallback for a camera the built-in one cannot handle).
    relay: Literal["native", "ffmpeg"] | None = None


class OnvifScan(BaseModel):
    scanned_at: float
    devices: list[dict] = Field(default_factory=list)

    @field_validator("devices", mode="before")
    @classmethod
    def _clean(cls, v):
        # Unauthenticated UDP replies from the site's LAN: cleaned, not trusted.
        return host_agent.clean_onvif_devices(v)


class ScannedDevice(BaseModel):
    ip: str = Field(..., max_length=15)
    ports: list[int] = Field(default_factory=list, max_length=16)
    rtsp: bool = False

    @field_validator("ip")
    @classmethod
    def _private_v4(cls, v):
        # What the agent's camera scan found on its own networks: only a
        # private IPv4 address makes sense (and is routed, site_relay).
        import ipaddress

        ip = ipaddress.IPv4Address(v)
        if not ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError("not a private IPv4 address")
        return str(ip)

    @field_validator("ports")
    @classmethod
    def _ports(cls, v):
        return [p for p in v if 0 < p < 65536]


class CameraScanReport(BaseModel):
    id: int
    scanned_at: float | None = None
    devices: list[ScannedDevice] = Field(default_factory=list, max_length=1024)
    subnets: list[str] = Field(default_factory=list, max_length=16)
    note: str | None = Field(None, max_length=300)


class SiteHealth(BaseModel):
    temp_c: float | None = Field(None, ge=-50, le=200)
    disk_free_mb: float | None = Field(None, ge=0)


class CheckItem(BaseModel):
    name: str = Field(..., max_length=120)
    ok: bool
    detail: str | None = Field(None, max_length=300)
    ms: int | None = Field(None, ge=0)


class ChecksReport(BaseModel):
    id: int
    at: float | None = None
    items: list[CheckItem] = Field(default_factory=list, max_length=200)


class SiteReport(BaseModel):
    version: str = Field(..., max_length=100)
    os: str = Field(..., max_length=40)
    arch: str = Field(..., max_length=40)
    hostname: str | None = Field(None, max_length=200)
    publishers: list[PublisherState] = Field(default_factory=list, max_length=512)
    onvif: OnvifScan | None = None
    # The camera scan an admin asked for (the agent's scan.go).
    scan: CameraScanReport | None = None
    # The agent's clock when it wrote this (Unix seconds), its machine's
    # health, and the checks an admin asked for (services/site_health.py).
    now: float | None = None
    health: SiteHealth | None = None
    checks: ChecksReport | None = None


def _require_site(
    request: Request,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> Site:
    site = svc.authenticate(db, _bearer(authorization))
    if site is None:
        raise _not_found()
    path = request.url.path
    if "/rpc/" in path:
        kind, limit = "rpc", RPC_RESULTS_PER_MINUTE
    elif "/publish-token/" in path:
        kind, limit = "token", TOKENS_PER_MINUTE
    else:
        kind, limit = "ctl", AGENT_PER_MINUTE
    if _rate_limited(f"site:{site.id}:{kind}", limit):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    return site


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _agent_answer(db: Session, site: Site, request: Request) -> dict:
    desired = svc.desired_state(db, site)
    return {
        "site_id": site.id,
        "desired": desired,
        "etag": svc.etag(desired, site),
        "live_policy": site.live_policy,
        "uplink_cap_kbps": site.uplink_cap_kbps,
        "ingest": svc.ingest_target(request.headers.get("host")),
        "poll_after_s": 5,
        "wait_max_s": svc.WAIT_MAX_S,
        "scan_request": svc.scan_request(site.id),
        "uplink_test": svc.uplink_test_request(site.id),
        "checks_request": site_health.checks_request(site.id),
    }


#: Agent downloads per client address per minute: one machine fetches one
#: build; this only stops a loop pulling ~15 MB files from the server.
DOWNLOADS_PER_MINUTE = 10


@agent_router.get("/download")
def list_downloads():
    """The agent builds this server ships (the same ones its own updates
    carry), with SHA-256 sums. Public: the agent is open source, and the
    site machine has no credential before it enrols."""
    return {"build": svc.agent_build(), "files": svc.agent_downloads()}


@agent_router.get("/download/{platform}")
def download_agent(platform: str, request: Request):
    from fastapi.responses import FileResponse

    found = svc.agent_binary(platform)
    if found is None:
        raise _not_found()
    if _rate_limited(f"download:{get_client_ip(request)}", DOWNLOADS_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    path, name = found
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})


@agent_router.post("/enrol")
async def enrol(body: EnrolRequest, request: Request, db: Session = Depends(get_db)):
    """Trade a one-time code for the site's credential (shown once).

    Re-enrolling a site (a new box, a reinstall) ends the streams the old
    agent is running: its credential stops working at once, but a running
    publish session is checked only when it starts."""
    from services.audit_service import audit_request
    from services.mediamtx_admin_service import MediaMtxAdminService

    ip = get_client_ip(request)
    if _rate_limited(f"enrol:{ip}", ENROL_PER_MINUTE) or _rate_limited("enrol:*", ENROL_GLOBAL_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    if body.token is not None:
        got = svc.enrol_with_link(db, body.token, ip=ip, version=body.version, hostname=body.hostname)
    else:
        got = svc.enrol(db, body.code, ip=ip, version=body.version, hostname=body.hostname)
    if got is None:
        raise _not_found()
    site, token, replaced = got
    kicked: dict = {}
    if replaced:
        kicked = await MediaMtxAdminService.kick_path_sessions(svc.site_paths(db, site))
    audit_request(db, request, action="site.enrolled", entity_type="site", entity_id=site.id,
                  details={"version": body.version, "hostname": body.hostname,
                           "credential_prefix": site.credential_prefix, "replaced": replaced,
                           "by": "setup_link" if body.token is not None else "code",
                           "approval_pending": bool(site.approval_pending_at),
                           "kicked": len(kicked.get("kicked") or [])})
    return {"site_id": site.id, "name": site.name, "token": token,
            "ingest": svc.ingest_target(request.headers.get("host"))}


class SetupInfoRequest(BaseModel):
    token_id: str = Field(..., min_length=4, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")


@agent_router.post("/setup-info")
def setup_info(body: SetupInfoRequest, request: Request, db: Session = Depends(get_db)):
    """What the public setup page shows for a link: the site's name, the
    downloads. POST, with only the link's id (its secret never leaves the
    browser), and it never uses the link up: mail and chat apps open links
    to preview them."""
    if _rate_limited(f"setup:{get_client_ip(request)}", SETUP_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    info = site_setup.setup_info(db, body.token_id)
    if info is None:
        raise _not_found()
    return {**info, "build": svc.agent_build(), "files": svc.agent_downloads()}


class HelloRequest(BaseModel):
    token_id: str = Field(..., min_length=4, max_length=16, pattern=r"^[A-Za-z0-9_-]+$")
    nonce: str = Field(..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@agent_router.post("/hello")
def hello(body: HelloRequest, request: Request, db: Session = Depends(get_db)):
    """The server proves to an agent with a setup link that it is the
    server that made the link, before the agent sends the link's secret
    (services/site_setup.py). Every answer looks the same."""
    if _rate_limited(f"setup:{get_client_ip(request)}", SETUP_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    return {"proof": site_setup.prove(db, body.token_id, body.nonce, svc.server_pin())}


class RepinRequest(BaseModel):
    site_id: int = Field(..., ge=1, le=2**31 - 1)
    nonce: str = Field(..., min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@agent_router.post("/repin")
def repin(body: RepinRequest, request: Request, db: Session = Depends(get_db)):
    """Prove this server's certificate key to an enrolled agent that met a
    new one (a renewed certificate): see services/sites.py repin_proof. It
    carries no credential, so a man in the middle learns nothing from the
    agent; every answer looks the same."""
    if _rate_limited(f"repin:{get_client_ip(request)}", REPIN_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    site = db.get(Site, body.site_id)
    credential = site.credential_hash if site is not None and site.revoked_at is None else None
    return {"proof": svc.repin_proof(credential, body.nonce, svc.server_pin())}


@agent_router.post("/sync")
def sync(report: SiteReport, request: Request, site: Site = Depends(_require_site),
         db: Session = Depends(get_db)):
    """The agent's report in, what to relay out."""
    svc.record_report(db, site, report.model_dump(), get_client_ip(request))
    return _agent_answer(db, site, request)


@agent_router.get("/wait")
async def wait(
    request: Request,
    etag: str = Query("", max_length=64),
    authorization: str | None = Header(None),
    site: Site = Depends(_require_site),
    db: Session = Depends(get_db),
):
    """Long poll: answers as soon as the desired state differs from ``etag``,
    or after WAIT_MAX_S with the same state.

    It holds NO database connection while it waits: the session is released
    first and used again after. (Held for the whole wait, ~30 polling sites
    took every connection of core's pool and the whole NVR stopped.) After
    the wait the credential is checked again, so an agent revoked or
    replaced meanwhile gets nothing more."""
    from core.database import release
    from services import site_relay

    site_id = site.id
    ev = svc.arm(site_id)  # before reading, so no change slips between
    answer = _agent_answer(db, site, request)
    if answer["etag"] == etag and not site_relay.has_pending(site_id):
        release(db)
        await svc.wait(ev)
        site = svc.authenticate(db, _bearer(authorization))
        if site is None or site.id != site_id:
            raise _not_found()  # deleted, revoked or replaced while waiting
        answer = _agent_answer(db, site, request)
    release(db)
    # HTTP requests core needs sent to this site's cameras (ONVIF, PTZ,
    # settings): the agent sends each and posts the answer to /rpc/{id}.
    answer["rpc"] = site_relay.take_pending(site_id)
    return answer


class RpcResult(BaseModel):
    status: int | None = Field(None, ge=100, le=599)
    headers: dict[str, list[str]] = Field(default_factory=dict)
    body_b64: str = Field("", max_length=6 * (1 << 20))  # 4 MiB, base64
    error: str | None = Field(None, max_length=500)


@agent_router.post("/rpc/{rpc_id}/result")
def rpc_result(rpc_id: str, body: RpcResult, site: Site = Depends(_require_site)):
    """The answer to one camera request this site's agent was handed."""
    from services import site_relay

    if not site_relay.complete(site.id, rpc_id[:32], body.model_dump()):
        raise _not_found()
    return {"ok": True}


@agent_router.get("/update")
def agent_update(site: Site = Depends(_require_site)):
    """The signed manifest of the agent builds this server ships, for the
    site's agent to check before it updates itself (it verifies the release
    signature itself: this server only relays it). 404 when the builds are
    unsigned."""
    import base64

    got = svc.agent_update()
    if got is None or not site.agent_updates:
        raise _not_found()  # unsigned builds, or updates switched off for this site
    manifest, sig = got
    return {"manifest": base64.b64encode(manifest).decode(), "signature": base64.b64encode(sig).decode()}


@agent_router.post("/speedtest/{test_id}")
async def speedtest(test_id: int, request: Request, site: Site = Depends(_require_site),
                    db: Session = Depends(get_db)):
    """The agent's upload for an uplink test an admin asked for (once): the
    bytes are counted and thrown away, and the rate is measured HERE, from
    the first byte to the last, not taken from the agent."""
    import time

    from core.database import release

    if not svc.uplink_test_wanted(site.id, test_id):
        raise _not_found()
    site_id = site.id
    release(db)
    n = 0
    started = None
    async for chunk in request.stream():
        if started is None:
            started = time.monotonic()
        n += len(chunk)
        if n > svc.UPLINK_TEST_MAX_BYTES + (1 << 20):
            break
    seconds = (time.monotonic() - started) if started else 0.0
    site = db.get(Site, site_id)
    result = svc.record_uplink_test(db, site, test_id, n, seconds)
    return result


@agent_router.post("/publish-token/{camera_id}")
def publish_token(camera_id: int, site: Site = Depends(_require_site), db: Session = Depends(get_db)):
    """A short publish token for one of this site's cameras, and nothing else."""
    cam = svc.camera_for_site(db, site, camera_id)
    if cam is None:
        raise _not_found()
    return svc.publish_token(cam)
