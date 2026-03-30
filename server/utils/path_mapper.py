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
Path mapping utilities for Docker volume mounts.

Converts between host filesystem paths and container filesystem paths
based on Docker volume mount configuration.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath

from core.config import settings


def host_to_container_path(host_path: str) -> str:
    """
    Convert a host filesystem path to a container filesystem path.

    This handles the Docker volume mount mapping, e.g.:
    - Host: d:/opennvr/Recordings/cam-1/2026/02/20/file.mp4
    - Container: /app/host-root/Recordings/cam-1/2026/02/20/file.mp4

    Args:
        host_path: Path on the host filesystem (can be Windows or Unix style)

    Returns:
        Equivalent path inside the container

    Example:
        >>> host_to_container_path("d:/opennvr/Recordings/cam-1")
        '/app/host-root/Recordings/cam-1'
    """
    # If not running in Docker (no host base configured), return the path as-is
    if not settings.recordings_host_base:
        return host_path or settings.recordings_base_path
    
    if not host_path:
        return settings.recordings_container_base

    # Normalize the paths for comparison - handle both Windows and Unix paths
    # Convert to absolute path and normalize separators
    try:
        # Try Windows path first
        host_path_obj = PureWindowsPath(host_path)
        host_base_obj = PureWindowsPath(settings.recordings_host_base)

        # Normalize to lowercase and forward slashes for comparison
        host_path_normalized = str(host_path_obj).replace("\\", "/").lower()
        host_base_normalized = str(host_base_obj).replace("\\", "/").lower()
    except (ValueError, TypeError):
        # If Windows path fails, try Unix path
        host_path_normalized = str(PurePosixPath(host_path)).lower()
        host_base_normalized = str(PurePosixPath(settings.recordings_host_base)).lower()

    # Check if the path is under the host base
    if not host_path_normalized.startswith(host_base_normalized):
        # Path doesn't match the mount point
        # Return just the container base path
        return settings.recordings_container_base

    # Get the relative path (the part after the base)
    relative_path = host_path_normalized[len(host_base_normalized) :].lstrip("/")

    # Combine with container base using POSIX path
    if relative_path:
        container_path = (
            PurePosixPath(settings.recordings_container_base) / relative_path
        )
    else:
        container_path = PurePosixPath(settings.recordings_container_base)

    return str(container_path)


def container_to_host_path(container_path: str) -> str:
    """
    Convert a container filesystem path to a host filesystem path.

    This is the reverse of host_to_container_path.

    Args:
        container_path: Path inside the container

    Returns:
        Equivalent path on the host filesystem

    Example:
        >>> container_to_host_path("/app/recordings/cam-1")
        'd:/opennvr/Recordings/cam-1'
    """
    # If not running in Docker (no host base configured), return the path as-is
    if not settings.recordings_host_base:
        return container_path or settings.recordings_base_path
    
    # Normalize the container path
    container_path_normalized = str(PurePosixPath(container_path))
    container_base_normalized = str(PurePosixPath(settings.recordings_container_base))

    # Check if the path is under the container base
    if not container_path_normalized.startswith(container_base_normalized):
        return settings.recordings_host_base

    # Get the relative path
    relative_path = container_path_normalized[len(container_base_normalized) :].lstrip(
        "/"
    )

    # Combine with host base using Windows path (if on Windows) or regular path
    if relative_path:
        host_path = Path(settings.recordings_host_base) / relative_path
    else:
        host_path = Path(settings.recordings_host_base)

    return str(host_path)


def get_mediamtx_recording_path(user_configured_path: str = None) -> str:
    """
    Get the MediaMTX recording path (container path) from user-configured path.

    If user configures a recording path in the UI, this converts it to the
    path that MediaMTX should use inside its container.

    Args:
        user_configured_path: Path configured by user (host path), or None to use default

    Returns:
        Container path for MediaMTX to use
    """
    if user_configured_path:
        return host_to_container_path(user_configured_path)
    else:
        return host_to_container_path(settings.recordings_base_path)
