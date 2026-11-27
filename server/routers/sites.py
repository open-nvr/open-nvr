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
#: Agent calls per site per minute: a sync every few seconds, a long poll,
#: and a token per stream (re)connect for main + sub of every camera.
AGENT_PER_MINUTE = 240


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


# ── admin ──────────────────────────────────────────────────────────────


class SiteCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class SiteUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    live_policy: Literal["always", "sub_only", "on_demand_main"] | None = None
    uplink_cap_kbps: int | None = Field(None, ge=0, le=10_000_000)


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


def _enrol_command(request: Request, code: str) -> str:
    host = svc.ingest_target(request.headers.get("host"))["host"] or "<this-server>"
    pin = svc.server_pin()
    # The pin lets a self-signed server be trusted from the first call;
    # with a Let's Encrypt certificate it is harmless (the chain verifies).
    pin_arg = f"-pin {pin} " if pin else ""
    return f"opennvr-host enrol {pin_arg}https://{host} {code}"


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
    return {**svc.public_view(db, site), "enrol_code": code, "enrol_expires_at": expires,
            "enrol_command": _enrol_command(request, code)}


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
    return {"enrol_code": code, "enrol_expires_at": expires,
            "enrol_command": _enrol_command(request, code)}


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


@router.put("/{site_id}/cameras/{camera_id}")
def link_camera(
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
    return svc.public_view(db, site)


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
    audit_request(db, request, action="site.camera_unlink", user_id=user.id, entity_type="camera",
                  entity_id=cam.id, details={"site_id": site.id})
    return svc.public_view(db, site)


# ── agent ──────────────────────────────────────────────────────────────


class EnrolRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=32)
    version: str | None = Field(None, max_length=100)
    hostname: str | None = Field(None, max_length=200)


class PublisherState(BaseModel):
    camera_id: int
    stream: Literal["main", "sub"] = "main"
    state: Literal["starting", "live", "error", "waiting", "uplink_cap"]
    detail: str | None = Field(None, max_length=300)
    since: float | None = None
    kbps: float | None = None


class OnvifScan(BaseModel):
    scanned_at: float
    devices: list[dict] = Field(default_factory=list)

    @field_validator("devices", mode="before")
    @classmethod
    def _clean(cls, v):
        # Unauthenticated UDP replies from the site's LAN: cleaned, not trusted.
        return host_agent.clean_onvif_devices(v)


class SiteReport(BaseModel):
    version: str = Field(..., max_length=100)
    os: str = Field(..., max_length=40)
    arch: str = Field(..., max_length=40)
    hostname: str | None = Field(None, max_length=200)
    publishers: list[PublisherState] = Field(default_factory=list, max_length=512)
    onvif: OnvifScan | None = None


def _require_site(
    request: Request,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
) -> Site:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    site = svc.authenticate(db, token)
    if site is None:
        raise _not_found()
    if _rate_limited(f"site:{site.id}", AGENT_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    return site


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
    }


@agent_router.post("/enrol")
def enrol(body: EnrolRequest, request: Request, db: Session = Depends(get_db)):
    """Trade a one-time code for the site's credential (shown once)."""
    from services.audit_service import audit_request

    ip = get_client_ip(request)
    if _rate_limited(f"enrol:{ip}", ENROL_PER_MINUTE):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")
    got = svc.enrol(db, body.code, ip=ip, version=body.version)
    if got is None:
        raise _not_found()
    site, token = got
    audit_request(db, request, action="site.enrolled", entity_type="site", entity_id=site.id,
                  details={"version": body.version, "hostname": body.hostname,
                           "credential_prefix": site.credential_prefix})
    return {"site_id": site.id, "name": site.name, "token": token,
            "ingest": svc.ingest_target(request.headers.get("host"))}


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
    site: Site = Depends(_require_site),
    db: Session = Depends(get_db),
):
    """Long poll: answers as soon as the desired state differs from ``etag``,
    or after WAIT_MAX_S with the same state."""
    from services import site_relay

    ev = svc.arm(site.id)  # before reading, so no change slips between
    answer = _agent_answer(db, site, request)
    if answer["etag"] == etag and not site_relay.has_pending(site.id):
        await svc.wait(ev)
        db.expire_all()
        site = db.get(Site, site.id)
        if site is None or not site.credential_hash:
            raise _not_found()  # deleted or revoked while waiting
        answer = _agent_answer(db, site, request)
    # HTTP requests core needs sent to this site's cameras (ONVIF, PTZ,
    # settings): the agent sends each and posts the answer to /rpc/{id}.
    answer["rpc"] = site_relay.take_pending(site.id)
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


@agent_router.post("/publish-token/{camera_id}")
def publish_token(camera_id: int, site: Site = Depends(_require_site), db: Session = Depends(get_db)):
    """A short publish token for one of this site's cameras, and nothing else."""
    cam = svc.camera_for_site(db, site, camera_id)
    if cam is None:
        raise _not_found()
    return svc.publish_token(cam)
