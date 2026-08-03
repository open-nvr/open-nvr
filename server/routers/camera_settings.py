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
Camera settings — read a camera's device configuration through its driver.

Phase 0: capabilities, device info, network (read-only), storage (read-only).
Every route resolves the camera via ``get_camera_or_403`` (ownership/superuser)
and loads the camera's stored, encrypted credentials server-side — it never
accepts credentials as query parameters (unlike the legacy /onvif router).

Access model: GET routes are readable by any user the camera resolves for
(owner or superuser). EVERY mutating route additionally requires the
``camera_device.write`` RBAC permission (superusers hold it implicitly) —
device settings are read-only for ordinary users. This covers the dangerous
operations in particular (on-camera account create/delete, reboot, config
export) but applies uniformly to all writes.

Deliberately NO endpoint changes the camera's IP/network and NO factory-reset
route exists — that safety property is enforced all the way down in the driver
abstraction, not just here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.database import get_db
from core.permissions import RequirePermission, get_camera_or_403
from models import Camera, CameraCapability, CameraEvent, User
from services.camera_drivers.capabilities import get_or_probe, probe_and_store
from services.audit_service import write_audit_log
from services.camera_drivers.registry import get_driver_for_camera
from services.camera_event_manager import get_camera_event_manager

router = APIRouter(prefix="/cameras", tags=["camera-settings"])

# Gate for every route that CHANGES the device (or exports its secrets).
# Superusers pass implicitly; other users need the permission granted to their
# role. Ordinary camera owners get read-only access to device settings.
require_device_write = RequirePermission("camera_device.write")


class ManualSyncRequest(BaseModel):
    timezone: str | None = None


class NtpRequest(BaseModel):
    server: str
    timezone: str | None = None


class ImagingUpdate(BaseModel):
    brightness: int | None = None
    contrast: int | None = None
    saturation: int | None = None
    sharpness: int | None = None
    ir_cut_filter: str | None = None  # ON | OFF | AUTO (day/night)
    wdr: str | None = None  # ON | OFF
    backlight: str | None = None  # ON | OFF
    # Vendor-only extras (Hikvision ISAPI); ignored by drivers that lack them.
    flip: str | None = None  # ON | OFF (rotate 180)
    noise_reduction: int | None = None  # 0-100
    noise_reduce_mode: str | None = None  # close | general | advanced


class EncoderUpdate(BaseModel):
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate: int | None = None
    gov_length: int | None = None


class OsdUpdate(BaseModel):
    datetime_enabled: bool | None = None
    channel_name_enabled: bool | None = None
    text_enabled: bool | None = None
    text: str | None = None
    # Multi-slot form: [{"id": 2, "enabled": true, "text": "Gate"}]
    slots: list[dict] | None = None


class ServiceUpdate(BaseModel):
    enabled: bool


class ConfigBackupRequest(BaseModel):
    # The camera encrypts the backup with this; the same key restores it.
    secret_key: str


class MotionUpdate(BaseModel):
    enabled: bool | None = None
    sensitivity: int | None = None


class IpFilterUpdate(BaseModel):
    enabled: bool
    mode: str = "allow"  # allow (whitelist, strong) | deny (blacklist, weak)
    entries: list[str] | None = None


class CloudUpdate(BaseModel):
    enabled: bool


class SmartUpdate(BaseModel):
    enabled: bool | None = None
    sensitivity: int | None = None


class CameraUserCreate(BaseModel):
    username: str
    password: str
    level: str = "Operator"  # Administrator | Operator | Viewer


async def _reconcile_stream(db: Session, camera: Camera) -> dict:
    """After a camera encoder change, bounce the MediaMTX path so it re-DESCRIBEs
    the (unchanged) source URL and picks up the new stream format. Best-effort:
    a failure here does NOT undo the encoder change on the camera."""
    from models import CameraConfig

    cfg = (
        db.query(CameraConfig)
        .filter(CameraConfig.camera_id == camera.id)
        .first()
    )
    if not cfg or not cfg.source_url:
        return {"reconciled": False, "reason": "camera not provisioned to a stream"}

    # Import the MediaMTX admin service only when a reconcile is actually
    # needed (not for the common not-provisioned early-return above).
    from services.mediamtx_admin_service import MediaMtxAdminService

    if not MediaMtxAdminService.is_configured():
        return {"reconciled": False, "reason": "MediaMTX admin API not configured"}
    try:
        result = await MediaMtxAdminService.push_rtsp_stream(
            camera_id=camera.id,
            camera_ip=camera.ip_address,
            rtsp_url=cfg.source_url,
            enable_recording=bool(cfg.recording_enabled),
            rtsp_transport=cfg.rtsp_transport or "tcp",
            recording_path=cfg.recording_path,
            transport_security=cfg.transport_security,
        )
        return {
            "reconciled": result.get("status") == "ok",
            "detail": result.get("action") or result.get("status"),
        }
    except Exception as e:  # never fail the encoder change on a reconcile error
        return {"reconciled": False, "reason": str(e)}


def _serialize_capability(row: CameraCapability) -> dict:
    return {
        "camera_id": row.camera_id,
        "driver_name": row.driver_name,
        "manufacturer": row.manufacturer,
        "model": row.model,
        "firmware_version": row.firmware_version,
        "onvif_endpoints": row.onvif_endpoints or {},
        "supported_areas": row.supported_areas or {},
        "capabilities": row.capabilities or {},
        "probe_result": row.probe_result,
        "probed_at": row.probed_at.isoformat() if row.probed_at else None,
        "probe_error": row.probe_error,
    }


@router.get("/{camera_id}/capabilities")
async def get_capabilities(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Cached capability snapshot (probes once if never probed)."""
    row = await get_or_probe(db, camera.id)
    return _serialize_capability(row)


@router.post("/{camera_id}/capabilities/refresh")
async def refresh_capabilities(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Force a fresh capability probe (also refreshes device metadata)."""
    row = await probe_and_store(db, camera.id)
    return _serialize_capability(row)


@router.get("/{camera_id}/info")
async def get_info(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Live device identity read (manufacturer/model/firmware/serial)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        info = await driver.get_info()
        return {"driver_name": driver.driver_name, **info.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Camera info read failed: {e}")


@router.get("/{camera_id}/network")
async def get_network(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """READ-ONLY network configuration. There is no corresponding PUT."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        net = await driver.get_network()
        return net.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Network read failed: {e}")


@router.get("/{camera_id}/storage")
async def get_storage(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """READ-ONLY SD/HDD storage status."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        storage = await driver.get_storage()
        return storage.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Storage read failed: {e}")


@router.get("/{camera_id}/imaging")
async def get_imaging(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read image settings (brightness/contrast/…, day-night, WDR) + ranges."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_imaging()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Imaging read failed: {e}")


@router.put("/{camera_id}/imaging")
async def set_imaging(
    payload: ImagingUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Apply image settings (only provided fields; values clamped to ranges)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        patch = payload.model_dump(exclude_none=True)
        return (await driver.set_imaging(patch)).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Imaging update failed: {e}")


@router.get("/{camera_id}/encoder")
async def get_encoder(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read the video-encoder configurations (main/sub) + their option ranges."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_encoder()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Encoder read failed: {e}")


@router.put("/{camera_id}/encoder/{config_token}")
async def set_encoder(
    config_token: str,
    payload: EncoderUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Apply encoder changes to one configuration, then reconcile the MediaMTX
    stream (brief reconnect). The camera-side change stands even if the
    reconcile step reports it couldn't refresh the stream."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        info = await driver.set_encoder(
            config_token, payload.model_dump(exclude_none=True)
        )
        reconcile = await _reconcile_stream(db, camera)
        return {**info.to_dict(), "reconcile": reconcile}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Encoder update failed: {e}")


@router.get("/{camera_id}/osd")
async def get_osd(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read on-screen overlays (date/time, channel name, custom text)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_osd()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OSD read failed: {e}")


@router.put("/{camera_id}/osd")
async def set_osd(
    payload: OsdUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Toggle the date/time + channel-name overlays and set a custom text line."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.set_osd(payload.model_dump(exclude_none=True))).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OSD update failed: {e}")


@router.get("/{camera_id}/motion")
async def get_motion(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read motion-detection state (enabled, sensitivity)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_motion()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Motion read failed: {e}")


@router.put("/{camera_id}/motion")
async def set_motion(
    payload: MotionUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Enable/disable motion detection and set its sensitivity."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.set_motion(payload.model_dump(exclude_none=True))).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Motion update failed: {e}")


@router.get("/{camera_id}/smart")
async def get_smart(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read analytics detectors (line crossing, intrusion, face, tamper)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_smart()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Smart read failed: {e}")


@router.put("/{camera_id}/smart/{detector_key}")
async def set_smart(
    detector_key: str,
    payload: SmartUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Enable/disable one analytics detector and set its sensitivity."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        patch = payload.model_dump(exclude_none=True)
        return (await driver.set_smart(detector_key, patch)).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Smart update failed: {e}")


@router.get("/{camera_id}/services")
async def get_camera_services(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read the camera's network services (UPnP, SNMP, FTP, DDNS, email, …)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_services()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Services read failed: {e}")


@router.put("/{camera_id}/services/{service_key}")
async def set_camera_service(
    service_key: str,
    payload: ServiceUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Enable/disable one network service on the camera."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        result = await driver.set_service(service_key, payload.enabled)
        write_audit_log(
            db,
            action="camera.service.update",
            entity_type="camera",
            entity_id=camera.id,
            details={"service": service_key, "enabled": payload.enabled},
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Service update failed: {e}")


@router.get("/{camera_id}/snapshot-still")
async def get_camera_snapshot_still(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """A JPEG still pulled straight from the camera via its native API."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return Response(content=await driver.get_snapshot(), media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Snapshot failed: {e}")


@router.post("/{camera_id}/config-backup")
async def export_camera_config(
    payload: ConfigBackupRequest,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Download the camera's full configuration blob (vendor backup format).
    Write-gated (``camera_device.write``): the blob can contain device secrets."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        blob = await driver.export_config(payload.secret_key)
        write_audit_log(
            db,
            action="camera.config.export",
            entity_type="camera",
            entity_id=camera.id,
            details={"bytes": len(blob)},
        )
        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition":
                    f'attachment; filename="camera-{camera.id}-config.bin"'
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Config export failed: {e}")


@router.get("/{camera_id}/security")
async def get_camera_security(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read camera-side security posture (IP filter, cloud, login lock,
    protocol/port state) plus the address this server reaches the camera from."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_security()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Security read failed: {e}")


@router.put("/{camera_id}/security/ip-filter")
async def set_camera_ip_filter(
    payload: IpFilterUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Write the camera's IP filter.

    LOCKOUT-CAPABLE. The driver enforces the guardrails: an ``allow`` list is
    refused if empty, this server's real source address is force-included and
    verified, the list is staged with the filter disabled and read back before
    being armed, and access is re-verified afterwards.
    """
    try:
        driver = await get_driver_for_camera(db, camera.id)
        result = await driver.set_ip_filter(
            enabled=payload.enabled,
            mode=payload.mode,
            entries=payload.entries or [],
        )
        write_audit_log(
            db,
            action="camera.ip_filter.update",
            entity_type="camera",
            entity_id=camera.id,
            details={
                "enabled": payload.enabled,
                "mode": payload.mode,
                "entries": result.ip_filter_entries,
            },
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"IP filter update failed: {e}")


@router.put("/{camera_id}/security/cloud")
async def set_camera_cloud(
    payload: CloudUpdate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Enable/disable the vendor P2P cloud tunnel (Hik-Connect / EZVIZ)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        result = await driver.set_cloud(payload.enabled)
        write_audit_log(
            db,
            action="camera.cloud.update",
            entity_type="camera",
            entity_id=camera.id,
            details={"enabled": payload.enabled},
        )
        return result.to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Cloud update failed: {e}")


@router.post("/{camera_id}/events/subscribe")
async def subscribe_events(
    camera: Camera = Depends(get_camera_or_403),
    _writer: User = Depends(require_device_write),
):
    """Start streaming this camera's native alarms into OpenNVR (motion/tamper).
    Ephemeral: subscriptions do not survive a server restart."""
    await get_camera_event_manager().start(camera.id)
    return {"camera_id": camera.id, "subscribed": True}


@router.post("/{camera_id}/events/unsubscribe")
async def unsubscribe_events(
    camera: Camera = Depends(get_camera_or_403),
    _writer: User = Depends(require_device_write),
):
    """Stop streaming this camera's alarms."""
    await get_camera_event_manager().stop(camera.id)
    return {"camera_id": camera.id, "subscribed": False}


@router.get("/{camera_id}/events/status")
async def events_status(
    camera: Camera = Depends(get_camera_or_403),
):
    return {
        "camera_id": camera.id,
        "subscribed": get_camera_event_manager().is_running(camera.id),
    }


@router.get("/{camera_id}/events")
async def list_events(
    limit: int = 50,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Recent camera-native alarm history (most recent first)."""
    limit = max(1, min(200, limit))
    rows = (
        db.query(CameraEvent)
        .filter(CameraEvent.camera_id == camera.id)
        .order_by(CameraEvent.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "event_state": r.event_state,
            "description": r.description,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        }
        for r in rows
    ]


@router.get("/{camera_id}/camera-users")
async def list_camera_users(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """List on-camera user accounts (the one OpenNVR uses is flagged)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_users()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"User list failed: {e}")


@router.post("/{camera_id}/camera-users")
async def create_camera_user(
    payload: CameraUserCreate,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Create a new on-camera account. Write-gated (``camera_device.write``):
    an on-camera Administrator account outlives OpenNVR itself."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (
            await driver.create_user(payload.username, payload.password, payload.level)
        ).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"User create failed: {e}")


@router.delete("/{camera_id}/camera-users/{username}")
async def delete_camera_user(
    username: str,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Delete an on-camera account. Write-gated (``camera_device.write``).
    Refuses the account OpenNVR itself uses."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.delete_user(username)).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"User delete failed: {e}")


@router.post("/{camera_id}/reboot")
async def reboot_camera(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Reboot the camera. Write-gated (``camera_device.write``): recording
    drops while the device restarts. Does NOT change any configuration."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return await driver.reboot()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Reboot failed: {e}")


@router.get("/{camera_id}/time")
async def get_time(
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
):
    """Read the camera clock (mode, UTC/local time, timezone, NTP server)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.get_time()).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Time read failed: {e}")


@router.post("/{camera_id}/time/sync-manual")
async def sync_time_manual(
    payload: ManualSyncRequest,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Push this server's current UTC to the camera (fixes a wrong clock)."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (await driver.set_time_manual(timezone=payload.timezone)).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Time sync failed: {e}")


@router.put("/{camera_id}/time/ntp")
async def set_time_ntp(
    payload: NtpRequest,
    camera: Camera = Depends(get_camera_or_403),
    db: Session = Depends(get_db),
    _writer: User = Depends(require_device_write),
):
    """Point the camera at an NTP server and switch it to NTP time."""
    try:
        driver = await get_driver_for_camera(db, camera.id)
        return (
            await driver.set_ntp(payload.server, timezone=payload.timezone)
        ).to_dict()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"NTP config failed: {e}")
