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
MediaMTX Admin Service

This module acts as an adapter to the MediaMTX Admin API v3 (see openapi.yaml).
Base must include /v3 (e.g., http://localhost:9997/v3).

Endpoints used:
- GET    /config/global                   Get global config
- PATCH  /config/global                   Update global config
- GET    /config/pathdefaults             Get path defaults
- PATCH  /config/pathdefaults             Update path defaults
- POST   /config/paths/add/{name}         Add path config, body: PathConf
- GET    /config/paths/get/{name}         Get path config
- PATCH  /config/paths/edit/{name}        Edit path config
- DELETE /config/paths/delete/{name}      Delete path config
- GET    /paths/list                      List active paths/streams
- GET    /paths/get/{name}                Get active path info
- GET    /recordings/list                 List recordings
- GET    /recordings/get/{name}/{segment} Get recording segment info
- DELETE /recordings/delete/{name}/{segment} Delete recording segment

This service maps our CameraConfig into PathConf fields and handles RTSP stream pushing.

NOTE: All public methods are async — callers must await them.
"""

from typing import Any

import httpx

from core.config import settings
from core.logging_config import mediamtx_logger
from services.storage_service import get_effective_recordings_base_path
from services.stream_service import _build_stream_name
from utils.path_mapper import get_mediamtx_recording_path

# Shared timeout for all MediaMTX Admin API calls (seconds)
_TIMEOUT = httpx.Timeout(10.0)

# Fields containing sensitive data (hooks with hardcoded secrets) - COMPLETELY HIDDEN from UI
SENSITIVE_HOOK_FIELDS = [
    "runOnInit",
    "runOnInitRestart",
    "runOnDemand",
    "runOnDemandRestart",
    "runOnDemandStartTimeout",
    "runOnDemandCloseAfter",
    "runOnUnDemand",
    "runOnConnect",
    "runOnConnectRestart",
    "runOnDisconnect",
    "runOnReady",
    "runOnReadyRestart",
    "runOnNotReady",
    "runOnRead",
    "runOnReadRestart",
    "runOnUnread",
    "runOnRecordSegmentCreate",
    "runOnRecordSegmentComplete",
]

# Infrastructure fields that should be READ-ONLY (not editable by users)
READ_ONLY_INFRASTRUCTURE_FIELDS = [
    # Authentication - managed by backend
    "authMethod",
    "authInternalUsers",
    "authHTTPAddress",
    "authHTTPExclude",
    "authJWTJWKS",
    "authJWTJWKSFingerprint",
    "authJWTClaimKey",
    "authJWTExclude",
    "authJWTInHTTPQuery",
    # Admin API - infrastructure only
    "api",
    "apiAddress",
    "apiEncryption",
    "apiServerKey",
    "apiServerCert",
    "apiAllowOrigins",
    "apiTrustedProxies",
    # Metrics - infrastructure only
    "metrics",
    "metricsAddress",
    "metricsEncryption",
    "metricsServerKey",
    "metricsServerCert",
    "metricsAllowOrigins",
    "metricsTrustedProxies",
    # PPROF - infrastructure only
    "pprof",
    "pprofAddress",
    "pprofEncryption",
    "pprofServerKey",
    "pprofServerCert",
    "pprofAllowOrigins",
    "pprofTrustedProxies",
    # Playback API - infrastructure only
    "playback",
    "playbackAddress",
    "playbackEncryption",
    "playbackServerKey",
    "playbackServerCert",
    "playbackAllowOrigins",
    "playbackTrustedProxies",
    # Network bindings - infrastructure only
    "rtspAddress",
    "rtspsAddress",
    "rtpAddress",
    "rtcpAddress",
    "srtpAddress",
    "srtcpAddress",
    "multicastIPRange",
    "multicastRTPPort",
    "multicastRTCPPort",
    "multicastSRTPPort",
    "multicastSRTCPPort",
    "rtspServerKey",
    "rtspServerCert",
    "rtmpAddress",
    "rtmpsAddress",
    "rtmpServerKey",
    "rtmpServerCert",
    "hlsAddress",
    "hlsServerKey",
    "hlsServerCert",
    "webrtcAddress",
    "webrtcServerKey",
    "webrtcServerCert",
    "webrtcLocalUDPAddress",
    "webrtcLocalTCPAddress",
    "webrtcIPsFromInterfaces",
    "webrtcIPsFromInterfacesList",
    "webrtcAdditionalHosts",
    "webrtcICEServers2",
    "srtAddress",
    # Logging - infrastructure only
    "logLevel",
    "logDestinations",
    "logFile",
    "sysLogPrefix",
]


def _filter_sensitive_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Remove sensitive hook fields from configuration before sending to frontend.
    These fields contain hardcoded secrets and should never be exposed to the UI.

    Args:
        config: Raw configuration from MediaMTX API

    Returns:
        Filtered configuration with sensitive fields removed
    """
    if not isinstance(config, dict):
        return config

    filtered = config.copy()

    # Remove all sensitive hook fields completely
    for field in SENSITIVE_HOOK_FIELDS:
        if field in filtered:
            del filtered[field]

    # Also remove read-only infrastructure fields to reduce clutter
    for field in READ_ONLY_INFRASTRUCTURE_FIELDS:
        if field in filtered:
            del filtered[field]

    return filtered


def _validate_patch_payload(payload: dict[str, Any]) -> None:
    """
    Validate that PATCH request doesn't try to modify protected fields.
    Raises HTTPException if forbidden fields are present.

    Args:
        payload: User-submitted configuration changes

    Raises:
        ValueError: If payload contains forbidden fields
    """
    if not isinstance(payload, dict):
        return

    # Check for sensitive hook fields
    forbidden_hooks = set(payload.keys()) & set(SENSITIVE_HOOK_FIELDS)
    if forbidden_hooks:
        raise ValueError(
            f"Cannot modify protected hook fields: {', '.join(sorted(forbidden_hooks))}. "
            f"These are managed internally by the system."
        )

    # Check for read-only infrastructure fields
    forbidden_infra = set(payload.keys()) & set(READ_ONLY_INFRASTRUCTURE_FIELDS)
    if forbidden_infra:
        raise ValueError(
            f"Cannot modify read-only infrastructure fields: {', '.join(sorted(forbidden_infra))}. "
            f"These are managed by system configuration."
        )


class MediaMtxAdminService:
    """Async HTTP client wrapper for MediaMTX admin API v3."""

    @staticmethod
    def _headers() -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.mediamtx_admin_token:
            headers["Authorization"] = f"Bearer {settings.mediamtx_admin_token}"
        return headers

    @staticmethod
    def is_configured() -> bool:
        return bool(settings.mediamtx_admin_api)

    @staticmethod
    def _base() -> str:
        return settings.mediamtx_admin_api.rstrip("/")

    @staticmethod
    def _normalize_record_path(path_value: str | None) -> str:
        """Ensure recordPath contains %path and a time placeholder.
        - Must contain %path (stream name)
        - Must contain either %s OR all of %Y %m %d %H %M %S and %f
        If missing, append required segments safely.
        """
        val = (path_value or "").strip()
        if not val:
            # Get user-configured or default recording path
            host_path = get_effective_recordings_base_path()
            container_path = get_mediamtx_recording_path(host_path)
            return f"{container_path}/%path/%Y/%m/%d/%H-%M-%S-%f"
        # ensure %path
        if "%path" not in val:
            if not val.endswith("/"):
                val += "/"
            val += "%path"
        # ensure time placeholder
        if "%s" in val:
            return val
        required = ["%Y", "%m", "%d", "%H", "%M", "%S"]
        if all(tok in val for tok in required):
            # Check if %f is present, if not add it
            if "%f" not in val:
                val += "-%f"
            return val
        # append time suffix with %f
        if not val.endswith("/"):
            val += "/"
        val += "%Y/%m/%d/%H-%M-%S-%f"
        return val

    @staticmethod
    def _map_conf(config: dict[str, Any]) -> dict[str, Any]:
        """Map our CameraConfig dict to MediaMTX PathConf schema (flat fields)."""
        conf: dict[str, Any] = {}
        source_url = config.get("source_url") or config.get("source")
        if source_url:
            conf["source"] = source_url
        rtsp_transport = config.get("rtsp_transport") or config.get("rtspTransport")
        if rtsp_transport:
            conf["rtspTransport"] = rtsp_transport
        # Recording
        recording_enabled = None
        record_path_value = None
        segment_seconds_value = None
        if "recording" in config and isinstance(config.get("recording"), dict):
            rec = config["recording"]
            recording_enabled = rec.get("enabled")
            record_path_value = rec.get("path")
            segment_seconds_value = rec.get("segment_seconds")
        else:
            record_path_value = config.get("recording_path")
            segment_seconds_value = config.get("recording_segment_seconds")
            recording_enabled = config.get("recording_enabled")
        if recording_enabled is not None:
            conf["record"] = bool(recording_enabled)
        if record_path_value is not None:
            conf["recordPath"] = MediaMtxAdminService._normalize_record_path(
                record_path_value
            )
        if segment_seconds_value:
            conf["recordSegmentDuration"] = f"{int(segment_seconds_value)}s"
        return conf

    # ------------------------------------------------------------------
    # Internal helper: convert an httpx.Response into our standard result
    # ------------------------------------------------------------------
    @staticmethod
    def _to_result(path: str, resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text}
        return {
            "path": path,
            "status": "ok" if resp.is_success else "error",
            "http_status": resp.status_code,
            "details": data,
        }

    # === GLOBAL CONFIGURATION ===

    @staticmethod
    async def global_get() -> dict[str, Any]:
        """Get global MediaMTX configuration (filtered to hide sensitive hooks and read-only fields)."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + "/config/global/get"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            result = MediaMtxAdminService._to_result("global", resp)

            # Filter sensitive and read-only fields before returning to frontend
            if result.get("status") == "ok" and "details" in result:
                result["details"] = _filter_sensitive_config(result["details"])

            return result
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    @staticmethod
    async def global_patch(payload: dict[str, Any]) -> dict[str, Any]:
        """Update global MediaMTX configuration (validates that protected fields are not modified)."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        # Validate that user isn't trying to modify protected fields
        try:
            _validate_patch_payload(payload)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        url = MediaMtxAdminService._base() + "/config/global/patch"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.patch(
                    url, json=payload, headers=MediaMtxAdminService._headers()
                )
            return MediaMtxAdminService._to_result("global", resp)
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    # === PATH DEFAULTS ===

    @staticmethod
    async def pathdefaults_get() -> dict[str, Any]:
        """Get path defaults configuration (filtered to hide sensitive hooks and read-only fields)."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + "/config/pathdefaults/get"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            result = MediaMtxAdminService._to_result("pathdefaults", resp)

            # Filter sensitive and read-only fields before returning to frontend
            if result.get("status") == "ok" and "details" in result:
                result["details"] = _filter_sensitive_config(result["details"])

            return result
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    @staticmethod
    async def pathdefaults_patch(payload: dict[str, Any]) -> dict[str, Any]:
        """Update path defaults configuration (validates that protected fields are not modified)."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        # Validate that user isn't trying to modify protected fields
        try:
            _validate_patch_payload(payload)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        url = MediaMtxAdminService._base() + "/config/pathdefaults/patch"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.patch(
                    url, json=payload, headers=MediaMtxAdminService._headers()
                )
            return MediaMtxAdminService._to_result("pathdefaults", resp)
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    # === PATH MANAGEMENT ===

    @staticmethod
    async def patch_path(
        camera_id: int, camera_ip: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Update specific path configuration (validates that protected fields are not modified)."""
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "message": "mediamtx_admin_api not configured",
            }

        # Validate that user isn't trying to modify protected fields
        try:
            _validate_patch_payload(payload)
        except ValueError as e:
            return {"status": "error", "path": name, "message": str(e)}

        url = MediaMtxAdminService._base() + f"/config/paths/patch/{name}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.patch(
                    url, json=payload, headers=MediaMtxAdminService._headers()
                )
            return MediaMtxAdminService._to_result(name, resp)
        except Exception as e:
            return {
                "status": "error",
                "path": name,
                "message": f"Request failed: {e!s}",
            }

    @staticmethod
    async def patch_path_by_name(
        path_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Update specific path configuration by path name directly."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": path_name,
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + f"/config/paths/patch/{path_name}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.patch(
                    url, json=payload, headers=MediaMtxAdminService._headers()
                )
            return MediaMtxAdminService._to_result(path_name, resp)
        except Exception as e:
            return {
                "status": "error",
                "path": path_name,
                "message": f"Request failed: {e!s}",
            }

    # === ACTIVE STREAMS ===

    @staticmethod
    async def list_active_paths() -> dict[str, Any]:
        """List all active paths/streams."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + "/paths/list"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result("paths", resp)
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    @staticmethod
    async def get_active_path(camera_id: int, camera_ip: str) -> dict[str, Any]:
        """Get active path/stream information."""
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + f"/paths/get/{name}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result(name, resp)
        except Exception as e:
            return {
                "status": "error",
                "path": name,
                "message": f"Request failed: {e!s}",
            }

    @staticmethod
    async def get_active_path_info(path_name: str) -> dict[str, Any]:
        """Get active path/stream information by path name."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": path_name,
                "message": "mediamtx_admin_api not configured; no-op",
            }
        url = MediaMtxAdminService._base() + f"/paths/get/{path_name}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result(path_name, resp)
        except Exception as e:
            return {
                "status": "error",
                "path": path_name,
                "message": f"Request failed: {e!s}",
            }

    # === RECORDING MANAGEMENT ===

    @staticmethod
    async def list_recordings(
        camera_id: int = None, camera_ip: str = None
    ) -> dict[str, Any]:
        """List all recordings or for a specific camera."""
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + "/recordings/list"
        if camera_id and camera_ip:
            path_name = _build_stream_name(
                settings.mediamtx_stream_prefix, camera_id, camera_ip
            )
            url += f"?path={path_name}"

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result("recordings", resp)
        except Exception as e:
            return {"status": "error", "message": f"Request failed: {e!s}"}

    @staticmethod
    async def get_recording_segment(
        camera_id: int, camera_ip: str, segment: str
    ) -> dict[str, Any]:
        """Get information about a specific recording segment."""
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "segment": segment,
                "message": "mediamtx_admin_api not configured",
            }

        url = MediaMtxAdminService._base() + f"/recordings/get/{name}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result(f"{name}/{segment}", resp)
        except Exception as e:
            return {
                "status": "error",
                "path": name,
                "segment": segment,
                "message": f"Request failed: {e!s}",
            }

    @staticmethod
    async def delete_recording_segment(
        camera_id: int, camera_ip: str, segment: str
    ) -> dict[str, Any]:
        """Delete a specific recording segment."""
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "segment": segment,
                "message": "mediamtx_admin_api not configured",
            }

        # MediaMTX uses query parameters for deletesegment endpoint
        url = (
            MediaMtxAdminService._base()
            + f"/recordings/deletesegment?path={name}&start={segment}"
        )
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.delete(url, headers=MediaMtxAdminService._headers())
            return MediaMtxAdminService._to_result(f"{name}/{segment}", resp)
        except Exception as e:
            return {
                "status": "error",
                "path": name,
                "segment": segment,
                "message": f"Request failed: {e!s}",
            }

    # === RTSP STREAM PUSHING ===

    @staticmethod
    async def push_rtsp_stream(
        camera_id: int,
        camera_ip: str,
        rtsp_url: str,
        enable_recording: bool = False,
        rtsp_transport: str = "tcp",
        recording_segment_seconds: int = 300,
        recording_path: str | None = None,
    ) -> dict[str, Any]:
        """Push RTSP stream to MediaMTX and optionally enable recording."""
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)

        # First provision the path with RTSP source
        config = {
            "source_url": rtsp_url,
            "rtsp_transport": rtsp_transport,
        }

        if enable_recording:
            # Use custom recording path if provided, otherwise use default
            if recording_path:
                # Use the custom path provided by user
                final_recording_path = recording_path
            else:
                # Get user-configured recording path and convert to container path
                host_path = get_effective_recordings_base_path()
                container_path = get_mediamtx_recording_path(host_path)
                final_recording_path = f"{container_path}/%path/%Y/%m/%d/%H-%M-%S-%f"

            config["recording"] = {
                "enabled": True,
                "path": final_recording_path,
                "segment_seconds": recording_segment_seconds,
                "format": "fmp4",  # Ensure format is fmp4
            }
        else:
            config["recording"] = {"enabled": False}

        result = await MediaMtxAdminService.provision_path(camera_id, camera_ip, config)

        # If path already exists, try to unprovision and re-provision
        if (
            result.get("status") == "error"
            and result.get("details", {}).get("error") == "path already exists"
        ):
            # First, unprovision the existing path
            unprovision_result = await MediaMtxAdminService.unprovision_path(
                camera_id, camera_ip
            )

            # Then try to provision again
            if unprovision_result.get("status") == "ok":
                result = await MediaMtxAdminService.provision_path(
                    camera_id, camera_ip, config
                )
                result["action"] = "rtsp_stream_replaced"
            else:
                result["action"] = "unprovision_failed"
                result["unprovision_result"] = unprovision_result

        if result.get("status") == "ok":
            if "action" not in result:
                result["action"] = "rtsp_stream_pushed"
            result["rtsp_url"] = rtsp_url
            result["recording_enabled"] = enable_recording
            result["rtsp_transport"] = rtsp_transport
            result["recording_segment_seconds"] = recording_segment_seconds

        return result

    # === ORIGINAL METHODS ===

    @staticmethod
    async def provision_path(
        camera_id: int, camera_ip: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)

        mediamtx_logger.log_action(
            "mediamtx.provision_path_start",
            camera_id=camera_id,
            message=f"Provisioning MediaMTX path for camera {camera_id}",
            extra_data={
                "camera_id": camera_id,
                "camera_ip": camera_ip,
                "path_name": name,
                "config": config,
            },
        )

        if not MediaMtxAdminService.is_configured():
            mediamtx_logger.log_action(
                "mediamtx.provision_path_no_config",
                camera_id=camera_id,
                message=f"MediaMTX admin API not configured for path: {name}",
                extra_data={"path": name},
            )
            return {
                "status": "no_admin_api",
                "path": name,
                "details": {
                    "message": "mediamtx_admin_api not configured; no-op",
                    "hint": "Set MEDIAMTX_ADMIN_API (e.g., http://localhost:9997/v3) to enable provisioning",
                },
            }

        url = MediaMtxAdminService._base() + f"/config/paths/add/{name}"
        payload = MediaMtxAdminService._map_conf(config)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    url, json=payload, headers=MediaMtxAdminService._headers()
                )
            result = MediaMtxAdminService._to_result(name, resp)

            if result.get("http_status") in [200, 201]:
                mediamtx_logger.log_action(
                    "mediamtx.provision_path_success",
                    camera_id=camera_id,
                    message=f"MediaMTX path provisioned successfully: {name}",
                    extra_data={
                        "path": name,
                        "http_status": result.get("http_status"),
                        "result": result,
                    },
                )
            else:
                mediamtx_logger.error(
                    f"MediaMTX path provisioning failed: {name}",
                    extra={
                        "camera_id": camera_id,
                        "path": name,
                        "http_status": result.get("http_status"),
                        "result": result,
                        "action": "mediamtx.provision_path_failed",
                    },
                )

            return result

        except httpx.ConnectError as e:
            # Handle connection errors (MediaMTX not running) more gracefully
            error_msg = f"MediaMTX connection failed: {e!s}"
            if "WinError 10061" in str(e) or "Connection refused" in str(e):
                error_msg = "MediaMTX service is not running or not accessible"

            mediamtx_logger.warning(
                f"MediaMTX connection error: {name}",
                extra={
                    "camera_id": camera_id,
                    "camera_ip": camera_ip,
                    "path": name,
                    "url": url,
                    "error_type": "ConnectError",
                    "action": "mediamtx.provision_path_connection_error",
                },
            )
            return {
                "status": "connection_error",
                "path": name,
                "message": error_msg,
                "details": {
                    "error_type": "ConnectError",
                    "hint": "Ensure MediaMTX is running and accessible at the configured URL",
                    "url": url,
                },
            }
        except httpx.TimeoutException as e:
            mediamtx_logger.warning(
                f"MediaMTX timeout error: {name}",
                extra={
                    "camera_id": camera_id,
                    "camera_ip": camera_ip,
                    "path": name,
                    "url": url,
                    "error_type": "Timeout",
                    "action": "mediamtx.provision_path_timeout",
                },
            )
            return {
                "status": "timeout",
                "path": name,
                "message": f"MediaMTX request timed out: {e!s}",
                "details": {
                    "error_type": "Timeout",
                    "hint": "MediaMTX may be overloaded or slow to respond",
                },
            }
        except Exception as e:
            mediamtx_logger.error(
                f"MediaMTX path provisioning error: {name}",
                extra={
                    "camera_id": camera_id,
                    "camera_ip": camera_ip,
                    "path": name,
                    "url": url,
                    "payload": payload,
                    "error_type": type(e).__name__,
                    "action": "mediamtx.provision_path_exception",
                },
                exc_info=True,
            )
            return {
                "status": "error",
                "path": name,
                "details": {"error": str(e), "error_type": type(e).__name__},
            }

    @staticmethod
    async def unprovision_path(camera_id: int, camera_ip: str) -> dict[str, Any]:
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "details": {"message": "mediamtx_admin_api not configured; no-op"},
            }
        url = MediaMtxAdminService._base() + f"/config/paths/delete/{name}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(url, headers=MediaMtxAdminService._headers())
        return MediaMtxAdminService._to_result(name, resp)

    @staticmethod
    async def path_status(camera_id: int, camera_ip: str) -> dict[str, Any]:
        name = _build_stream_name(settings.mediamtx_stream_prefix, camera_id, camera_ip)
        if not MediaMtxAdminService.is_configured():
            return {
                "status": "no_admin_api",
                "path": name,
                "details": {"message": "mediamtx_admin_api not configured; no-op"},
            }
        url = MediaMtxAdminService._base() + f"/config/paths/get/{name}"
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, headers=MediaMtxAdminService._headers())
        return MediaMtxAdminService._to_result(name, resp)

    @staticmethod
    async def enable_recording(
        camera_id: int, duration: str = "60s", segment_duration: str = "10s"
    ) -> dict[str, Any]:
        """Enable recording for a camera stream."""
        from core.database import SessionLocal
        from models import Camera

        with SessionLocal() as db:
            cam = db.query(Camera).filter(Camera.id == camera_id).first()
            if not cam:
                return {"status": "error", "detail": "Camera not found"}

            # Get user-configured recording path and convert to container path
            host_path = get_effective_recordings_base_path()
            container_path = get_mediamtx_recording_path(host_path)

            # Create recording configuration payload
            recording_config = {
                "record": True,
                "recordPath": f"{container_path}/%path/%Y/%m/%d/%H-%M-%S-%f",
                "recordFormat": "fmp4",
                "recordSegmentDuration": duration,
                "recordDeleteAfter": "168h",  # 7 days default
            }

            return await MediaMtxAdminService.patch_path(
                camera_id, cam.ip_address, recording_config
            )

    @staticmethod
    async def disable_recording(camera_id: int) -> dict[str, Any]:
        """Disable recording for a camera stream."""
        from core.database import SessionLocal
        from models import Camera

        with SessionLocal() as db:
            cam = db.query(Camera).filter(Camera.id == camera_id).first()
            if not cam:
                return {"status": "error", "detail": "Camera not found"}

            # Disable recording
            recording_config = {"record": False}

            return await MediaMtxAdminService.patch_path(
                camera_id, cam.ip_address, recording_config
            )

    @staticmethod
    async def get_recording_status(camera_id: int, db) -> dict[str, Any]:
        """Get current recording status for a camera."""
        from models import Camera

        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if not cam:
            return {"recording_enabled": False, "message": "Camera not found"}

        path_info = await MediaMtxAdminService.get_active_path(
            camera_id, cam.ip_address
        )

        # Extract recording status from path configuration
        if path_info and path_info.get("status") == "ok" and "details" in path_info:
            conf = path_info["details"]
            return {
                "camera_id": camera_id,
                "recording_enabled": conf.get("record", False),
                "record_path": conf.get("recordPath"),
                "record_format": conf.get("recordFormat", "mp4"),
                "segment_duration": conf.get("recordPartDuration", "10s"),
                "total_duration": conf.get("recordSegmentDuration", "60s"),
                "delete_after": conf.get("recordDeleteAfter", "168h"),
            }

        return {
            "camera_id": camera_id,
            "recording_enabled": False,
            "message": "Stream not active or configuration not available",
        }
