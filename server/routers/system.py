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
System control endpoints (shutdown/reboot).

Guarded by superuser. In debug mode, actions are NO-OP for safety and only log/acknowledge.
"""

import platform
import subprocess

from fastapi import APIRouter, Depends, HTTPException

from core.auth import get_current_superuser
from core.config import settings

router = APIRouter(prefix="/system", tags=["system"])  # mounted at /api/v1


def _run_command(cmd: list[str]):
    # Security: Whitelist allowed executables to prevent command injection
    allowed_executables = {"shutdown", "reboot", "sudo"}
    if not cmd or cmd[0] not in allowed_executables:
        raise HTTPException(
            status_code=500, detail="Security Violation: Unauthorized command"
        )

    try:
        # Security: shell=False prevents shell injection attacks
        subprocess.Popen(
            cmd, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to execute command: {e}")


@router.post("/shutdown")
async def shutdown(current_user=Depends(get_current_superuser)):
    if settings.debug:
        return {"accepted": True, "message": "Shutdown requested (debug mode: no-op)"}
    system = platform.system().lower()
    if system.startswith("win"):
        _run_command(["shutdown", "/s", "/t", "0"])
    else:
        _run_command(["sudo", "shutdown", "-h", "now"])
    return {"accepted": True}


@router.post("/reboot")
async def reboot(current_user=Depends(get_current_superuser)):
    if settings.debug:
        return {"accepted": True, "message": "Reboot requested (debug mode: no-op)"}
    system = platform.system().lower()
    if system.startswith("win"):
        _run_command(["shutdown", "/r", "/t", "0"])
    else:
        _run_command(["sudo", "reboot"])
    return {"accepted": True}
