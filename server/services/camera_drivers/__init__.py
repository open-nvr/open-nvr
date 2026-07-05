# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Multi-vendor camera device drivers (ONVIF baseline + vendor extensions)."""

from .base import (
    CameraDriver,
    Capabilities,
    DeviceInfo,
    NetworkInfo,
    StorageInfo,
)
from .registry import get_driver_for_camera, select_driver_class

__all__ = [
    "CameraDriver",
    "Capabilities",
    "DeviceInfo",
    "NetworkInfo",
    "StorageInfo",
    "get_driver_for_camera",
    "select_driver_class",
]
