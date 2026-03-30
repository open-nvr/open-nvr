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

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.config import settings

WSL_FASTLOG_PATH = settings.suricata_fastlog_path


async def stream_suricata_logs(request: Request):
    # Async stream of new lines from fast.log with cancellation awareness
    last_size = 0
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                if not os.path.exists(WSL_FASTLOG_PATH):
                    yield "[Suricata fast.log not found]\n"
                    await asyncio.sleep(2)
                    continue
                with open(WSL_FASTLOG_PATH, encoding="utf-8", errors="ignore") as f:
                    f.seek(last_size)
                    while True:
                        if await request.is_disconnected():
                            return
                        line = f.readline()
                        if line:
                            yield line
                        else:
                            last_size = f.tell()
                            await asyncio.sleep(1)
                            break
            except Exception as e:
                if await request.is_disconnected():
                    break
                yield f"[Error reading fast.log: {e}]\n"
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        # Graceful cancellation on shutdown
        return


router = APIRouter()


@router.get("/suricata/alerts/stream")
def get_suricata_alerts_stream(request: Request):
    """
    Streams Suricata alerts from WSL to the frontend.
    """
    return StreamingResponse(stream_suricata_logs(request), media_type="text/plain")
