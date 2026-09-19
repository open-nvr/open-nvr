"""HA-601 spike check: Home Assistant's own ``onvif`` integration against
scripts/spikes/onvif_server.py. Run by scripts/spikes/onvif-ha-check.ps1.

1. mints a read-only OpenNVR token and starts the ONVIF spike with it;
2. adds the ONVIF device in a fresh Home Assistant (config flow, manual);
3. checks the cameras (one per profile), a snapshot through HA, live video
   (HA's stream component on the RTSP URI), and that PullPoint events became
   binary sensors with a state;
4. prints every ONVIF operation HA called. The server's log is
   onvif_server.log next to this file's working directory.

Environment: HA_URL, NVR_URL, NVR_JWT, ONVIF_HOST_FROM_HA (host.docker.internal).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ha-dev"))
from e2e_ha import HA, HAClient, check, nvr, results  # noqa: E402

HOST = os.environ.get("ONVIF_HOST_FROM_HA", "host.docker.internal")
PORT = 8099


async def main() -> int:
    async with aiohttp.ClientSession() as s:
        ha = HAClient(s)
        await ha.wait_up()
        await ha.onboard()
        tok = await nvr(s, "POST", "/api-tokens", json={
            "name": f"onvif-spike-{int(time.time())}", "expires_in_days": 1,
            "scopes": ["cameras.view", "live.view"]})
        env = {**os.environ, "NVR_TOKEN": tok["token"], "PUBLIC_HOST": HOST,
               "PORT": str(PORT), "SPIKE_TRACE": "1", "ONVIF_USER": "ha", "ONVIF_PASS": "spike-pass"}
        log = open("onvif_server.log", "w", encoding="utf-8")  # noqa: SIM115
        server = subprocess.Popen(
            ["uv", "run", "--quiet", "--no-project", "--with", "aiohttp", "python",
             os.path.join(os.path.dirname(__file__), "onvif_server.py")], env=env,
            stdout=log, stderr=subprocess.STDOUT)
        try:
            await asyncio.sleep(6)
            flow = await ha.post("/api/config/config_entries/flow", {"handler": "onvif"})
            flow = await ha.post(f"/api/config/config_entries/flow/{flow['flow_id']}",
                                 {"auto": False})
            print("onvif flow step:", flow.get("step_id"))
            async with s.post(f"{HA}/api/config/config_entries/flow/{flow['flow_id']}",
                              json={"name": "OpenNVR (ONVIF)", "host": HOST, "port": PORT,
                                    "username": "ha", "password": "spike-pass"},
                              headers=ha.h) as r:
                flow = await r.json()
            if flow.get("type") == "form" and flow.get("step_id") == "configure_profile":
                flow = await ha.post(f"/api/config/config_entries/flow/{flow['flow_id']}", {})
            check("HA's onvif integration accepts OpenNVR as an ONVIF device",
                  flow.get("type") == "create_entry",
                  str(flow.get("errors") or flow.get("description_placeholders") or ""))
            await asyncio.sleep(15)

            ents = [e for e in await ha.ws({"type": "config/entity_registry/list"})
                    if e.get("platform") == "onvif"]
            cams = [e["entity_id"] for e in ents if e["entity_id"].startswith("camera.")]
            sensors = [e["entity_id"] for e in ents
                       if e["entity_id"].startswith("binary_sensor.")]
            check("a camera per OpenNVR camera", len(cams) >= 2, f"{len(cams)}: {cams[:6]}")

            async with s.get(f"{HA}/api/camera_proxy/{cams[0]}", headers=ha.h) as r:
                body = await r.read()
            check("a snapshot through HA", r.status == 200 and body[:2] == b"\xff\xd8",
                  f"HTTP {r.status}, {len(body)} bytes")

            stream = await ha.ws({"type": "camera/stream", "entity_id": cams[0]})
            playlist = ""
            for _ in range(20):
                async with s.get(f"{HA}{stream['url']}") as r:
                    playlist = await r.text() if r.status == 200 else ""
                if "#EXT" in playlist:
                    break
                await asyncio.sleep(2)
            check("live video through HA's stream component (RTSPS + JWT URI)",
                  "#EXTM3U" in playlist, " ".join(playlist[:80].split()))

            states = await ha.states()
            valued = {e: states.get(e, {}).get("state") for e in sensors}
            check("PullPoint events become binary sensors with a state",
                  any(v in ("on", "off") for v in valued.values()),
                  f"{len(sensors)} sensors, e.g. {dict(list(valued.items())[:4])}")

            async with s.get(f"http://localhost:{PORT}/spike/calls") as r:
                calls = await r.json()
            print("ONVIF operations HA called:", sorted(set(calls)))
        finally:
            # uv starts python as a child: end the whole tree (Windows).
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)],
                               capture_output=True, check=False)
            else:
                server.terminate()
            log.close()
            await nvr(s, "DELETE", f"/api-tokens/{tok['id']}")
            await ha.close_ws()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    print(f"Home Assistant: {HA}")
    sys.exit(asyncio.run(main()))
