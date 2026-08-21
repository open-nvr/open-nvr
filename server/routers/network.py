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
Network router for Camera LAN and Uplink configuration.

Stores settings in SecuritySetting keys and exposes computed lists like whitelisted
IPs (provisioned cameras) and blacklisted IPs.

Also provides an endpoint to create a firewall rule to isolate the Camera LAN
from the internet (DB-only rule; OS-level enforcement depends on an external agent).
"""

import ipaddress
import json
import os
import socket
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import Camera, CameraConfig, FirewallRule, SecuritySetting
from schemas import NetworkConfig
from services.audit_service import write_audit_log

router = APIRouter(prefix="/network", tags=["network"])


def get_camera_lan_subnet(db: Session) -> str | None:
    """Return the primary Camera LAN subnet CIDR, or None if not set."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "network_camera_lan").first()
    if not row:
        return None
    cfg = {**_default_camera_lan(), **_load_json(row)}
    return cfg.get("subnet_cidr") or cfg.get("ipv4_address") or None


def get_camera_lan_subnets(db: Session) -> list[str]:
    """Return all configured Camera LAN scan subnets (primary + additional)."""
    row = db.query(SecuritySetting).filter(SecuritySetting.key == "network_camera_lan").first()
    if not row:
        return []
    cfg = {**_default_camera_lan(), **_load_json(row)}
    subnets: list[str] = []
    primary = cfg.get("subnet_cidr") or cfg.get("ipv4_address")
    if primary:
        subnets.append(primary)
    for extra in cfg.get("scan_subnets") or []:
        if extra and extra not in subnets:
            subnets.append(extra)
    return subnets


def _docker_excluded_networks() -> list[ipaddress.IPv4Network]:
    """Networks that must never be treated as a camera LAN inside a container:
    the compose bridge subnet (OPENNVR_DOCKER_SUBNET, default matches
    docker-compose.yml)."""
    raw = os.environ.get("OPENNVR_DOCKER_SUBNET") or "172.28.0.0/16"
    nets: list[ipaddress.IPv4Network] = []
    for part in raw.replace(",", " ").split():
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            nets.append(net)
    return nets


def detect_local_subnets() -> list[str]:
    """Best-effort auto-detection of the host's own IPv4 /24 subnet(s), so ONVIF
    discovery can run without the operator manually configuring a Camera LAN
    subnet. Standard library only.

    Enumerates *every* local IPv4 interface (a multi-NIC NVR typically has a
    separate camera-LAN interface distinct from its default/uplink route), plus
    the default-route address, then keeps only private (RFC 1918) /24s — never a
    loopback, link-local (169.254.x), or public range.

    Inside a Docker container the socket probes only see the bridge network
    (e.g. 172.28.0.x), which is never a camera LAN — those addresses are
    excluded, and the launcher-exported host addresses stand in for the host's
    real subnets: OPENNVR_HOST_IP (the default-route LAN IP, also used for the
    TLS cert SAN) plus OPENNVR_LAN_IPS (every LAN-facing NIC on a multi-NIC
    host, e.g. a dedicated camera network on a second adapter). Returns [] if
    nothing usable is found (e.g. a bridge container without either)."""
    in_docker = os.path.exists("/.dockerenv")
    ips: list[str] = []

    # Launcher-refreshed hint file, freshest source first: the launchers
    # rewrite it on every run, so a host that moved to a new subnet is picked
    # up without recreating the container — the env vars below are frozen at
    # container creation.
    hints_file = os.environ.get("OPENNVR_HOST_IPS_FILE") or "/app/net-hints/host-ips"
    try:
        with open(hints_file, encoding="utf-8") as fh:
            for token in fh.read().replace(",", " ").split():
                if token not in ips:
                    ips.append(token)
    except OSError:
        pass

    # The host's own LAN addresses, when the launcher passed them in. Listed
    # ahead of the probes so the default-route subnet becomes the primary
    # detected one. Skipped entirely when the hint file spoke: the launcher
    # writes the union of these env values into the file on every run, so
    # once hints exist the frozen env can only ADD subnets the host has since
    # left (e.g. the WiFi network it was on at container creation).
    if not ips:
        for env_key in ("OPENNVR_HOST_IP", "OPENNVR_LAN_IPS"):
            for token in (os.environ.get(env_key) or "").replace(",", " ").split():
                if token not in ips:
                    ips.append(token)

    # Default-route interface — reliable even where the hostname doesn't resolve
    # to every address (some containers). A UDP "connect" selects the egress
    # interface without sending packets, so it works offline too.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            if s.getsockname()[0] not in ips:
                ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # Every address the host's own name resolves to — covers extra NICs such as
    # a dedicated camera LAN that isn't on the default route.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in ips:
                ips.append(info[4][0])
    except OSError:
        pass

    excluded = _docker_excluded_networks() if in_docker else []

    subnets: list[str] = []
    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version != 4:
            continue
        if not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        if any(addr in net for net in excluded):
            continue
        cidr = str(ipaddress.ip_network(f"{ip}/24", strict=False))
        if cidr not in subnets:
            subnets.append(cidr)
    return subnets


def lan_cidr_for_ip(ip: str) -> str | None:
    """The /24 around ``ip`` when it's a usable private LAN address, else None.

    Same filtering as detect_local_subnets: IPv4, RFC 1918, never loopback,
    link-local, or (inside a container) a Docker network. Used to turn the
    requesting browser's address into a live scan suggestion — unlike the
    env-derived host hints, the client's own subnet can never be stale."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if addr.version != 4:
        return None
    if not addr.is_private or addr.is_loopback or addr.is_link_local:
        return None
    if os.path.exists("/.dockerenv") and any(addr in net for net in _docker_excluded_networks()):
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def _get_or_init(
    db: Session, key: str, default_value: dict[str, Any]
) -> SecuritySetting:
    row = db.query(SecuritySetting).filter(SecuritySetting.key == key).first()
    if not row:
        row = SecuritySetting(key=key, json_value=json.dumps(default_value))
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _load_json(row: SecuritySetting) -> dict[str, Any]:
    try:
        return json.loads(row.json_value or "{}")
    except Exception:
        return {}


def _default_camera_lan() -> dict[str, Any]:
    return {
        "interface_name": "eth0",
        "dhcp_enabled": True,
        "ipv4_address": None,
        "ipv4_subnet_mask": None,
        "ipv4_gateway": None,
        "preferred_dns": None,
        "alternate_dns": None,
        "mtu": 1500,
        "description": "Isolated camera network (no internet).",
        "subnet_cidr": None,
        "scan_subnets": [],
    }


def _default_uplink() -> dict[str, Any]:
    return {
        "interface_name": "eth1",
        "dhcp_enabled": True,
        "ipv4_address": None,
        "ipv4_subnet_mask": None,
        "ipv4_gateway": None,
        "preferred_dns": None,
        "alternate_dns": None,
        "mtu": 1500,
        "description": "External uplink to the internet.",
        "blacklisted_ips": [],
    }


@router.get("/camera-lan")
async def get_camera_lan(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    val = {**_default_camera_lan(), **_load_json(row)}
    # Compute whitelist from provisioned cameras (cameras with a config row)
    cam_rows = (
        db.query(Camera).join(CameraConfig, Camera.id == CameraConfig.camera_id).all()
    )
    whitelist = sorted({c.ip_address for c in cam_rows if c.ip_address})
    return {"settings": val, "whitelisted_ips": whitelist}


@router.put("/camera-lan")
async def update_camera_lan(
    payload: NetworkConfig,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    current_val = {**_default_camera_lan(), **_load_json(row)}

    # Convert payload to dict, excluding None to simulate PATCH behavior if needed,
    # but here we might want to allow setting to None.
    # NetworkConfig has defaults.
    payload_dict = payload.model_dump(exclude_unset=True)

    # Shallow merge
    for k, v in payload_dict.items():
        if k in current_val:
            current_val[k] = v

    row.json_value = json.dumps(current_val)
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="camera-lan",
            details=payload_dict,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}


@router.post("/camera-lan/isolate")
async def isolate_camera_lan(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Create a high-priority firewall rule that denies outbound traffic from the camera LAN subnet.

    Note: This stores the rule in DB; applying it at OS level requires an external agent.
    """
    row = _get_or_init(db, "network_camera_lan", _default_camera_lan())
    cfg = {**_default_camera_lan(), **_load_json(row)}
    subnet = cfg.get("subnet_cidr") or cfg.get("ipv4_address")
    if not subnet:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide subnet_cidr or ipv4_address in Camera LAN settings",
        )

    rule = FirewallRule(
        name="Isolate Camera LAN",
        direction="outbound",
        protocol="any",
        port_from=None,
        port_to=None,
        sources=subnet,
        action="deny",
        enabled=True,
        priority=10,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="firewall_rule",
            entity_id=rule.id,
            details={"auto": True, "reason": "camera-lan isolate"},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"status": "ok", "rule": {"id": rule.id, "name": rule.name}}


@router.get("/uplink")
async def get_uplink(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    row = _get_or_init(db, "network_uplink", _default_uplink())
    val = {**_default_uplink(), **_load_json(row)}
    return {"settings": val}


@router.put("/uplink")
async def update_uplink(
    payload: NetworkConfig,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    row = _get_or_init(db, "network_uplink", _default_uplink())
    current_val = {**_default_uplink(), **_load_json(row)}

    payload_dict = payload.model_dump(exclude_unset=True)

    for k, v in payload_dict.items():
        if k in current_val:
            current_val[k] = v

    row.json_value = json.dumps(current_val)
    db.commit()
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="uplink",
            details=payload_dict,
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}
    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="network",
            entity_id="uplink",
            details=payload or {},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass
    return {"settings": current_val}
