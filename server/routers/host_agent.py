# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Routes for the host agent (services/host_agent.py).

Two for the agent itself — sync and publish-token — authenticated by the
site's agent key and refused when relayed by the reverse proxy; one for the
web app, behind ``cameras.manage``.

Every agent-route failure is the same 404, so a caller learns nothing about
whether the key, the route or the camera was wrong.
"""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission
from models import User
from services import host_agent

router = APIRouter(prefix="/host-agent", tags=["host-agent"])
_require_cameras_manage = RequirePermission("cameras.manage")


# ── Report schema: bounded, so a buggy agent cannot bloat core ─────────


class Webcam(BaseModel):
    id: str = Field(..., min_length=1, max_length=300)
    name: str = Field(..., max_length=200)


class Ffmpeg(BaseModel):
    found: bool = False
    version: str | None = Field(None, max_length=200)


class PublisherState(BaseModel):
    camera_id: int
    state: Literal["starting", "live", "error", "device_missing", "waiting"]
    detail: str | None = Field(None, max_length=300)
    since: float | None = None


class Disk(BaseModel):
    mount: str = Field(..., max_length=300)
    total_bytes: int = Field(..., ge=0)
    free_bytes: int = Field(..., ge=0)


class Temperature(BaseModel):
    label: str = Field(..., max_length=100)
    celsius: float


class Metrics(BaseModel):
    cpu_percent: float | None = None
    cpu_count: int | None = None
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    load_avg: list[float] | None = Field(None, max_length=3)
    disks: list[Disk] = Field(default_factory=list, max_length=64)
    temperatures: list[Temperature] = Field(default_factory=list, max_length=64)
    recordings_disk: Disk | None = None


class OnvifScan(BaseModel):
    """The agent's last WS-Discovery round.

    Its devices came from unauthenticated UDP replies on the LAN, so they are
    cleaned (bad entries dropped, fields clamped, at most 256 kept) rather
    than validated: a strict schema would let any device on the network make
    core refuse the agent's whole sync — webcams and metrics with it.
    """

    scanned_at: float
    devices: list[dict] = Field(default_factory=list)

    @field_validator("devices", mode="before")
    @classmethod
    def _clean(cls, v):
        return host_agent.clean_onvif_devices(v)


class SyncReport(BaseModel):
    version: str = Field(..., max_length=100)
    role: Literal["user", "system"] = "user"
    os: str = Field(..., max_length=40)
    arch: str = Field(..., max_length=40)
    hostname: str | None = Field(None, max_length=200)
    ffmpeg: Ffmpeg = Field(default_factory=Ffmpeg)
    webcams: list[Webcam] = Field(default_factory=list, max_length=64)
    publishers: list[PublisherState] = Field(default_factory=list, max_length=64)
    metrics: Metrics | None = None
    onvif: OnvifScan | None = None


# ── Agent auth ─────────────────────────────────────────────────────────


#: Calls per client per minute. The agent syncs every few seconds and asks
#: for a token on each stream (re)connect; this leaves room for both and
#: stops a key-guessing loop.
_PER_MINUTE = 60
_calls: dict[str, list[float]] = {}


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    calls = [t for t in _calls.get(key, []) if now - t < 60]
    limited = len(calls) >= _PER_MINUTE
    if not limited:
        calls.append(now)
    _calls[key] = calls
    if len(_calls) > 1024:  # never grow without bound
        for k in [k for k, v in _calls.items() if not v or now - v[-1] >= 60]:
            _calls.pop(k, None)
    return limited


def _require_agent(
    request: Request,
    x_host_agent_key: str | None = Header(None),
) -> None:
    peer = request.client.host if request.client else "?"
    if host_agent.came_through_proxy(request.headers) or not host_agent.verify_key(
        x_host_agent_key
    ):
        # An unauthenticated caller only ever sees a 404 — the limit below
        # must not reveal the route on the 61st try. Key guessing is bounded
        # by the hash check's cost and the 404 itself, not by a counter.
        raise HTTPException(status_code=404, detail="Not Found")
    # Per peer AND per route: a burst of publish-token retries (ten webcams
    # whose ffmpeg keeps exiting) must not spend the sync's budget.
    if _rate_limited(f"{peer}:{request.url.path.rsplit('/', 1)[-1][:16]}"):
        raise HTTPException(status_code=429, detail="Too many requests; slow down")


# ── Routes ─────────────────────────────────────────────────────────────


@router.post("/sync", dependencies=[Depends(_require_agent)])
def sync(report: SyncReport, request: Request, db: Session = Depends(get_db)):
    """The agent's poll: what it sees in, what it should do out."""
    from services.audit_service import audit_request

    payload = report.model_dump()
    if host_agent.record_report(db, payload):
        audit_request(
            db, request, action="host_agent.connected", entity_type="host_agent",
            details={"version": report.version, "os": report.os, "arch": report.arch,
                     "hostname": report.hostname},
        )
    return {
        "core_build": host_agent.core_build(),
        "desired": host_agent.desired_state(db),
        "poll_after_s": host_agent.POLL_AFTER_S,
        "discover_onvif": host_agent.discovery_pending(),
    }


@router.post("/publish-token/{camera_id}", dependencies=[Depends(_require_agent)])
def publish_token(camera_id: int, db: Session = Depends(get_db)):
    """A short publish-only token, for a camera the agent was told to feed
    and for nothing else."""
    camera = host_agent.camera_is_desired(db, camera_id)
    if camera is None:
        raise HTTPException(status_code=404, detail="Not Found")
    stream_name, token = host_agent.publish_token(camera, include_sub=True)
    return {"camera_id": camera_id, "stream_name": stream_name, "token": token}


@router.get("")
def get_host_agent(
    db: Session = Depends(get_db),
    current_user: User = Depends(_require_cameras_manage),
):
    """The agent as the Add Camera dialog sees it: is it running, which
    webcams are plugged in, which camera each one feeds."""
    return host_agent.public_view(db)
