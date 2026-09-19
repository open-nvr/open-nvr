"""Probe: can MediaMTX /playback/get serve footage from the segment it is still writing?

This decides how soon after an event a notification clip is ready (HA-007 /
HA-113). It runs INSIDE the core container, so it uses core's own playback
client, URL and credentials:

    docker cp scripts/ha-dev/probe_playback.py opennvr_core:/tmp/probe_playback.py
    docker exec opennvr_core /app/server-venv/bin/python /tmp/probe_playback.py 1
    docker exec opennvr_core rm -f /tmp/probe_playback.py

Argument: the camera id (default 1). Read-only: it lists segments and fetches
a few seconds of footage; it changes nothing.

Result on 2026-09-18 (MediaMTX 1.15.4, fmp4, 1 s parts, 60 s segments): HTTP
200 video/mp4 for windows starting 20, 12 and 6 s ago. The in-progress
segment is playable, so a clip is ready ~2 s after the event ends.
"""

import asyncio
import sys
import warnings
from datetime import UTC, datetime, timedelta

# Existing SQLAlchemy relationship-overlap warnings are noise for this probe.
warnings.filterwarnings("ignore")

sys.path.insert(0, "/app/server")

from core.config import settings  # noqa: E402
from core.database import SessionLocal  # noqa: E402
from models import Camera  # noqa: E402
from services import mediamtx_client as mc  # noqa: E402
from services.stream_service import _build_stream_name  # noqa: E402


async def main(camera_id: int) -> None:
    db = SessionLocal()
    try:
        cam = db.query(Camera).filter(Camera.id == camera_id).first()
        if cam is None:
            raise SystemExit(f"camera {camera_id} not found")
        path = _build_stream_name(settings.mediamtx_stream_prefix, cam.id, cam.ip_address)
    finally:
        db.close()

    now = datetime.now(UTC)
    segs = await mc.list_segments(path, now - timedelta(minutes=3), now, use_cache=False)
    print("path", path, "segments (last 3 min):")
    for seg in (segs or [])[-3:]:
        print("  ", seg)

    client = mc.get_client()
    for back, dur in ((20, 15), (12, 8), (6, 4)):
        start = (datetime.now(UTC) - timedelta(seconds=back)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        params = {"path": path, "start": start, "duration": str(dur), **mc.playback_auth()}
        r = await client.get(f"{settings.mediamtx_playback_url}/get", params=params, timeout=20)
        print(f"now-{back}s for {dur}s -> HTTP {r.status_code}, {len(r.content)} bytes, "
              f"type={r.headers.get('content-type')}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1))
