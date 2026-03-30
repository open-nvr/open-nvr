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
MediaMTX Configuration Service

This service provides utilities for configuring MediaMTX with proper webhook endpoints
and recording settings. It generates the configuration snippets needed for MediaMTX
to integrate with this application.
"""

from typing import Any

from core.config import settings
from services.storage_service import get_effective_recordings_base_path


class MediaMtxConfigService:
    """Service for generating MediaMTX configuration."""

    @staticmethod
    def generate_webhook_config(base_url: str) -> dict[str, Any]:
        """Generate webhook configuration for MediaMTX.

        Args:
            base_url: Base URL of this application (e.g., "http://localhost:8000")

        Returns:
            Dict containing webhook configuration for MediaMTX
        """
        webhook_base = f"{base_url}{settings.api_prefix}/mediamtx/hooks"
        token_param = (
            f"?t={settings.mediamtx_webhook_token}"
            if settings.mediamtx_webhook_token
            else ""
        )

        return {
            "runOnRecordSegmentCreate": f"{webhook_base}/segment-create{token_param}",
            "runOnRecordSegmentComplete": f"{webhook_base}/segment-complete{token_param}",
            # Add other hooks as needed
            "runOnReady": None,  # Can be configured later
            "runOnNotReady": None,
            "runOnRead": None,
            "runOnReadRemove": None,
            "runOnPublish": None,
            "runOnPublishRemove": None,
        }

    @staticmethod
    def generate_recording_config(
        camera_id: int, camera_ip: str = None
    ) -> dict[str, Any]:
        """Generate recording configuration for a camera path.

        Args:
            camera_id: Camera ID
            camera_ip: Camera IP address (optional)

        Returns:
            Dict containing recording configuration
        """
        from services.stream_service import _build_stream_name

        stream_name = _build_stream_name(
            settings.mediamtx_stream_prefix, camera_id, camera_ip
        )
        base_path = get_effective_recordings_base_path()

        return {
            "record": True,
            "recordPath": f"{base_path}/{stream_name}/%Y/%m/%d/%H/%M/%S",
            "recordFormat": "mp4",  # or "ts" for transport stream
            "recordSegmentDuration": "5m",  # 5-minute segments
            "recordDeleteAfter": "720h",  # Keep recordings for 30 days
        }

    @staticmethod
    def generate_global_config() -> dict[str, Any]:
        """Generate global MediaMTX configuration recommendations.

        Returns:
            Dict containing global configuration settings
        """
        base_path = get_effective_recordings_base_path()

        return {
            # API settings
            "api": True,
            "apiAddress": f":{settings.mediamtx_api_port}",
            # Recording settings
            "recordPath": base_path + "/%path/%Y/%m/%d/%H/%M/%S",
            "recordFormat": "mp4",
            "recordSegmentDuration": "5m",
            "recordDeleteAfter": "720h",  # 30 days
            # Performance settings
            "readTimeout": "10s",
            "writeTimeout": "10s",
            "readBufferCount": 512,
            "writeBufferCount": 512,
            # RTSP settings
            "rtspAddress": f":{settings.mediamtx_rtsp_port}",
            "rtspTransport": "tcp",
            "rtspRangeType": "clock",
            # WebRTC settings (for WHEP playback)
            "webrtcAddress": f":{settings.mediamtx_webrtc_port}",
            "webrtcICEServers": [{"urls": ["stun:stun.l.google.com:19302"]}],
            # HLS settings (optional)
            "hlsAddress": f":{settings.mediamtx_hls_port}",
            "hlsAllow": "all",
            "hlsVariant": "mpegts",
        }

    @staticmethod
    def generate_path_defaults() -> dict[str, Any]:
        """Generate default path configuration.

        Returns:
            Dict containing default path settings
        """
        webhook_config = MediaMtxConfigService.generate_webhook_config(
            settings.get_application_url()
        )
        base_path = get_effective_recordings_base_path()

        return {
            # Source settings
            "source": "",  # Will be set per path
            "rtspTransport": "tcp",
            # Recording settings (disabled by default, enabled per camera)
            "record": False,
            "recordPath": base_path + "/%path/%Y/%m/%d/%H/%M/%S",
            "recordFormat": "mp4",
            "recordSegmentDuration": "5m",
            "recordDeleteAfter": "720h",
            # Webhook settings
            **{k: v for k, v in webhook_config.items() if v is not None},
            # Playback settings
            "readUser": "",
            "readPass": "",
            "publishUser": "",
            "publishPass": "",
        }

    @staticmethod
    def generate_mediamtx_yml(application_base_url: str | None = None) -> str:
        """Generate a complete MediaMTX YAML configuration.

        Args:
            application_base_url: Base URL of this application (auto-detected if not provided)

        Returns:
            YAML configuration string
        """
        if not application_base_url:
            application_base_url = settings.get_application_url()

        global_config = MediaMtxConfigService.generate_global_config()
        path_defaults = MediaMtxConfigService.generate_path_defaults()

        # Update webhook URLs with correct base URL
        webhook_config = MediaMtxConfigService.generate_webhook_config(
            application_base_url
        )
        path_defaults.update({k: v for k, v in webhook_config.items() if v is not None})

        yaml_content = f"""# MediaMTX Configuration for OpenNVR NVR
# Generated automatically - do not edit manually

# Global settings
api: {str(global_config["api"]).lower()}
apiAddress: "{global_config["apiAddress"]}"

# RTSP settings
rtspAddress: "{global_config["rtspAddress"]}"

# WebRTC settings (for WHEP playback)
webrtcAddress: "{global_config["webrtcAddress"]}"
webrtcICEServers:
"""

        for ice_server in global_config["webrtcICEServers"]:
            yaml_content += f"""  - urls: {ice_server["urls"]}\n"""

        yaml_content += f"""
# HLS settings (optional)
hlsAddress: "{global_config["hlsAddress"]}"
hlsAllow: "{global_config["hlsAllow"]}"
hlsVariant: "{global_config["hlsVariant"]}"

# Performance settings
readTimeout: "{global_config["readTimeout"]}"
writeTimeout: "{global_config["writeTimeout"]}"
readBufferCount: {global_config["readBufferCount"]}
writeBufferCount: {global_config["writeBufferCount"]}

# Global recording settings
recordPath: "{global_config["recordPath"]}"
recordFormat: "{global_config["recordFormat"]}"
recordSegmentDuration: "{global_config["recordSegmentDuration"]}"
recordDeleteAfter: "{global_config["recordDeleteAfter"]}"

# Path defaults
pathDefaults:
  # Source settings
  rtspTransport: "{path_defaults["rtspTransport"]}"
  
  # Recording (disabled by default)
  record: {str(path_defaults["record"]).lower()}
  recordPath: "{path_defaults["recordPath"]}"
  recordFormat: "{path_defaults["recordFormat"]}"
  recordSegmentDuration: "{path_defaults["recordSegmentDuration"]}"
  recordDeleteAfter: "{path_defaults["recordDeleteAfter"]}"
  
  # Webhook settings
"""

        if path_defaults.get("runOnRecordSegmentCreate"):
            yaml_content += f"""  runOnRecordSegmentCreate: "{path_defaults["runOnRecordSegmentCreate"]}"\n"""
        if path_defaults.get("runOnRecordSegmentComplete"):
            yaml_content += f"""  runOnRecordSegmentComplete: "{path_defaults["runOnRecordSegmentComplete"]}"\n"""

        yaml_content += """
# Paths will be configured dynamically via API
paths: {}
"""

        return yaml_content

    @staticmethod
    def get_setup_instructions(
        application_base_url: str | None = None,
    ) -> dict[str, Any]:
        """Get setup instructions for MediaMTX integration.

        Args:
            application_base_url: Base URL of this application (auto-detected if not provided)

        Returns:
            Dict containing setup instructions and configuration
        """
        if not application_base_url:
            application_base_url = settings.get_application_url()

        admin_api_url = f"http://localhost:{settings.mediamtx_api_port}/v3"
        webhook_token = settings.mediamtx_webhook_token or "your-secure-webhook-token"

        return {
            "overview": "Complete setup guide for MediaMTX integration with automatic camera provisioning",
            "features": [
                "Automatic camera re-provisioning on MediaMTX restart",
                "Database-driven stream management",
                "Recording webhook integration",
                "Admin API for stream control",
            ],
            "critical_setup": {
                "startup_hook": {
                    "description": "Essential for automatic camera provisioning after MediaMTX restarts",
                    "config": f'runOnInit: curl -X GET "{application_base_url}{settings.api_prefix}/mediamtx/startup/hook?delay=5&t={webhook_token}"',
                    "importance": "Without this, cameras won't be re-provisioned automatically after MediaMTX restart",
                }
            },
            "instructions": [
                "1. Install MediaMTX from https://github.com/bluenviron/mediamtx",
                "2. Create a mediamtx.yml configuration file using the provided YAML",
                "3. Add the startup hook to automatically provision cameras on restart",
                "4. Configure webhook token for security",
                "5. Start MediaMTX with: ./mediamtx mediamtx.yml",
                "6. Configure your application environment variables:",
                f"   - MEDIAMTX_ADMIN_API={admin_api_url}",
                f"   - MEDIAMTX_WEBHOOK_TOKEN={webhook_token}",
                f"   - RECORDINGS_BASE_PATH={settings.recordings_base_path}",
                "7. Test the integration using the API endpoints",
            ],
            "configuration_yaml": MediaMtxConfigService.generate_complete_mediamtx_yml(
                application_base_url
            ),
            "environment_variables": {
                "MEDIAMTX_ADMIN_API": admin_api_url,
                "MEDIAMTX_WEBHOOK_TOKEN": webhook_token,
                "RECORDINGS_BASE_PATH": settings.recordings_base_path,
            },
            "api_endpoints": {
                "Auto-Provisioning": {
                    "Startup Hook (Called by MediaMTX)": f"{application_base_url}{settings.api_prefix}/mediamtx/startup/hook",
                    "Manual Provision All": f"{application_base_url}{settings.api_prefix}/mediamtx/startup/provision-all",
                    "Provision Single Camera": f"{application_base_url}{settings.api_prefix}/mediamtx/startup/provision-camera/{{camera_id}}",
                    "Get Provisioning Status": f"{application_base_url}{settings.api_prefix}/mediamtx/startup/status",
                },
                "Stream Management": {
                    "Push RTSP Stream": f"{application_base_url}{settings.api_prefix}/mediamtx/admin/streams/push/{{camera_id}}",
                    "List Active Streams": f"{application_base_url}{settings.api_prefix}/mediamtx/admin/paths/list",
                    "List Recordings": f"{application_base_url}{settings.api_prefix}/mediamtx/admin/recordings/list",
                    "Global Config": f"{application_base_url}{settings.api_prefix}/mediamtx/admin/global",
                },
                "Webhook Endpoints": {
                    "Segment Create": f"{application_base_url}{settings.api_prefix}/mediamtx/hooks/segment-create",
                    "Segment Complete": f"{application_base_url}{settings.api_prefix}/mediamtx/hooks/segment-complete",
                },
            },
            "testing": {
                "manual_provision_all": f'curl -X POST "{application_base_url}{settings.api_prefix}/mediamtx/startup/provision-all" -H "Authorization: Bearer YOUR_TOKEN"',
                "check_status": f'curl -X GET "{application_base_url}{settings.api_prefix}/mediamtx/startup/status" -H "Authorization: Bearer YOUR_TOKEN"',
                "test_webhook": f'curl -X GET "{application_base_url}{settings.api_prefix}/mediamtx/startup/hook?t={webhook_token}"',
            },
            "troubleshooting": {
                "common_issues": [
                    {
                        "problem": "Cameras not provisioning on startup",
                        "solutions": [
                            "Check MediaMTX logs for startup hook execution",
                            "Verify webhook token matches environment variable",
                            "Test manual provisioning endpoint",
                            "Ensure MediaMTX admin API is accessible",
                        ],
                    },
                    {
                        "problem": "Startup hook not executing",
                        "solutions": [
                            "Verify runOnInit is in global settings section of mediamtx.yml",
                            "Check curl is available on MediaMTX host system",
                            "Test webhook URL manually with curl",
                            "Review MediaMTX startup logs for error messages",
                        ],
                    },
                ]
            },
        }

    @staticmethod
    def generate_complete_mediamtx_yml(application_base_url: str | None = None) -> str:
        """Generate a complete MediaMTX YAML configuration with startup hooks.

        Args:
            application_base_url: Base URL of this application (auto-detected if not provided)

        Returns:
            Complete YAML configuration string with startup hooks
        """
        if not application_base_url:
            application_base_url = settings.get_application_url()

        webhook_token = settings.mediamtx_webhook_token or "your-secure-webhook-token"
        webhook_base = f"{application_base_url}{settings.api_prefix}/mediamtx"
        base_path = get_effective_recordings_base_path()

        yaml_content = f"""# MediaMTX Configuration for OpenNVR NVR
# Generated automatically with startup hooks for camera auto-provisioning

###############################################
# Global settings

# API settings - REQUIRED for camera provisioning
api: yes
apiAddress: :9997

# Startup hook - CRITICAL for automatic camera provisioning after restart
runOnInit: curl -X GET "{webhook_base}/startup/hook?delay=5&t={webhook_token}"
runOnInitRestart: no

# RTSP settings
rtsp: yes
rtspAddress: :8554
rtspTransports: [tcp, udp]

# WebRTC settings (for WHEP playback)
webrtc: yes
webrtcAddress: :8889

# HLS settings (optional)
hls: yes
hlsAddress: :8888

# Performance settings
readTimeout: 10s
writeTimeout: 10s
writeQueueSize: 512

###############################################
# Path defaults - applied to all camera streams

pathDefaults:
  # Source settings (will be overridden per camera)
  source: publisher
  rtspTransport: tcp
  
  # Recording settings (disabled by default, enabled per camera)
  record: no
  recordPath: {base_path}/%path/%Y/%m/%d/%H-%M-%S-%f
  recordFormat: fmp4
  recordSegmentDuration: 5m
  recordDeleteAfter: 168h  # 7 days
  
  # Webhook integration for recording events
  runOnRecordSegmentCreate: curl -X GET "{webhook_base}/hooks/segment-create?path=$MTX_PATH&segment_path=$MTX_SEGMENT_PATH&t={webhook_token}"
  runOnRecordSegmentComplete: curl -X GET "{webhook_base}/hooks/segment-complete?path=$MTX_PATH&segment_path=$MTX_SEGMENT_PATH&segment_duration=$MTX_SEGMENT_DURATION&t={webhook_token}"

###############################################
# Paths - Camera streams will be provisioned automatically via API
# No need to configure individual cameras here - they're managed by your NVR system

paths: {{}}

###############################################
# Authentication (optional) 
# Uncomment and configure if needed

# authMethod: internal
# authInternalUsers:
#   - user: admin
#     pass: your-password
#     permissions:
#       - action: publish
#       - action: read
#       - action: api
"""

        return yaml_content
