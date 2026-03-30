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
Firmware and system update router.

Provides endpoints for:
- System information (OS, kernel, BIOS)
- Security update status
- Auto-update configuration
- Manual update checks and application
"""

import json
import platform
import subprocess
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from core.auth import get_current_superuser
from core.database import get_db
from models import SecuritySetting
from services.audit_service import write_audit_log

router = APIRouter(prefix="/firmware", tags=["firmware"])


def _get_system_info() -> dict[str, Any]:
    """Gather basic system information."""
    try:
        info = {
            "os": platform.system(),
            "os_version": platform.version(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "platform": platform.platform(),
            "bios": "Unknown",  # BIOS info requires platform-specific commands
            "distro": "Unknown",
        }

        # Try to get distribution info on Linux
        if info["os"] == "Linux":
            try:
                with open("/etc/os-release") as f:
                    for line in f:
                        if line.startswith("PRETTY_NAME="):
                            info["distro"] = line.split("=", 1)[1].strip().strip('"')
                            break
            except Exception:
                pass

        return info
    except Exception:
        return {
            "os": "Unknown",
            "kernel": "Unknown",
            "bios": "Unknown",
            "distro": "Unknown",
        }


def _check_updates() -> dict[str, Any]:
    """Check for available updates (platform-specific)."""
    try:
        os_type = platform.system()
        if os_type == "Linux":
            # Try apt (Debian/Ubuntu)
            try:
                result = subprocess.run(
                    ["apt", "list", "--upgradable"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    upgradable = [
                        line for line in lines if "/" in line and "upgradable" in line
                    ]
                    return {
                        "available": len(upgradable) > 0,
                        "count": len(upgradable),
                        "packages": upgradable[:10],  # Limit to first 10
                        "method": "apt",
                    }
            except Exception:
                pass

            # Try yum/dnf (RHEL/CentOS/Fedora)
            for cmd in ["dnf", "yum"]:
                try:
                    result = subprocess.run(
                        [cmd, "check-update"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    # check-update returns 100 if updates are available
                    if result.returncode == 100:
                        lines = result.stdout.strip().split("\n")
                        return {
                            "available": True,
                            "count": len(
                                [
                                    l
                                    for l in lines
                                    if l.strip() and not l.startswith("Last")
                                ]
                            ),
                            "packages": lines[:10],
                            "method": cmd,
                        }
                    elif result.returncode == 0:
                        return {
                            "available": False,
                            "count": 0,
                            "packages": [],
                            "method": cmd,
                        }
                except Exception:
                    continue

        elif os_type == "Windows":
            # Windows Update via PowerShell (requires PSWindowsUpdate module)
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-Command",
                        "Get-WUList | Select-Object Title | ConvertTo-Json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0 and result.stdout.strip():
                    updates = json.loads(result.stdout)
                    count = (
                        len(updates)
                        if isinstance(updates, list)
                        else (1 if updates else 0)
                    )
                    return {
                        "available": count > 0,
                        "count": count,
                        "packages": updates[:10]
                        if isinstance(updates, list)
                        else [updates]
                        if updates
                        else [],
                        "method": "windows-update",
                    }
            except Exception:
                pass

        return {"available": False, "count": 0, "packages": [], "method": "unknown"}

    except Exception:
        return {"available": False, "count": 0, "packages": [], "method": "error"}


def _get_auto_update_settings(db: Session) -> dict[str, Any]:
    """Get auto-update configuration from database."""
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == "firmware_auto_update")
        .first()
    )
    if not row:
        # Default: enabled for both Linux and Windows as requested
        default = {"enabled": True, "schedule": "daily", "reboot_if_required": False}
        row = SecuritySetting(
            key="firmware_auto_update", json_value=json.dumps(default)
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    try:
        return json.loads(row.json_value or "{}")
    except Exception:
        return {"enabled": True, "schedule": "daily", "reboot_if_required": False}


@router.get("/system-info")
async def get_system_info(current_user=Depends(get_current_superuser)):
    """Get system information (OS, kernel, BIOS, etc.)."""
    return _get_system_info()


@router.get("/update-status")
async def get_update_status(current_user=Depends(get_current_superuser)):
    """Check for available security/system updates."""
    return _check_updates()


@router.get("/auto-update")
async def get_auto_update_settings_endpoint(
    db: Session = Depends(get_db), current_user=Depends(get_current_superuser)
):
    """Get auto-update configuration."""
    return _get_auto_update_settings(db)


@router.put("/auto-update")
async def update_auto_update_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user=Depends(get_current_superuser),
    request: Request = None,
):
    """Update auto-update configuration."""
    row = (
        db.query(SecuritySetting)
        .filter(SecuritySetting.key == "firmware_auto_update")
        .first()
    )
    if not row:
        row = SecuritySetting(key="firmware_auto_update", json_value="{}")
        db.add(row)

    current_settings = _get_auto_update_settings(db)
    # Merge with payload
    for key, value in (payload or {}).items():
        if key in ["enabled", "schedule", "reboot_if_required"]:
            current_settings[key] = value

    row.json_value = json.dumps(current_settings)
    db.commit()

    try:
        write_audit_log(
            db,
            action="settings.update",
            user_id=current_user.id,
            entity_type="firmware",
            entity_id="auto_update",
            details=payload or {},
            ip=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        pass

    return current_settings


@router.post("/check-updates")
async def check_updates_manual(current_user=Depends(get_current_superuser)):
    """Manually trigger update check."""
    return _check_updates()


@router.post("/apply-updates")
async def apply_updates(current_user=Depends(get_current_superuser)):
    """Apply available updates (requires elevated privileges)."""
    try:
        os_type = platform.system()
        if os_type == "Linux":
            # Try apt first
            try:
                result = subprocess.run(
                    ["sudo", "apt", "upgrade", "-y"],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0:
                    return {
                        "status": "success",
                        "method": "apt",
                        "output": result.stdout,
                    }
                else:
                    return {"status": "error", "method": "apt", "error": result.stderr}
            except Exception:
                pass

            # Try dnf/yum
            for cmd in ["dnf", "yum"]:
                try:
                    result = subprocess.run(
                        ["sudo", cmd, "update", "-y"],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if result.returncode == 0:
                        return {
                            "status": "success",
                            "method": cmd,
                            "output": result.stdout,
                        }
                except Exception:
                    continue

        elif os_type == "Windows":
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-Command",
                        "Install-WindowsUpdate -AcceptAll -AutoReboot:$false",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                return {
                    "status": "success" if result.returncode == 0 else "error",
                    "method": "windows-update",
                    "output": result.stdout,
                }
            except Exception as e:
                return {"status": "error", "method": "windows-update", "error": str(e)}

        return {"status": "error", "error": "Unsupported platform"}

    except Exception as e:
        return {"status": "error", "error": str(e)}
