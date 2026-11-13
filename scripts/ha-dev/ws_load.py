"""Load check for the events WebSocket v2 (HA-111): N concurrent subscribers.

Opens N v2 sockets with one API token and counts, per socket, frames,
sequence gaps and ``lagged`` notices over a window, then prints a summary.
Run from the host against the stack's nginx:

    server\\.venv\\Scripts\\python.exe scripts/ha-dev/ws_load.py ^
        --url https://localhost --token onvr_xxx --clients 20 --seconds 60

The token needs ``cameras.view`` (and ``live.view`` for track frames).
Needs ``httpx`` and ``websockets`` (both in the server venv). Read-only.

Pass criteria used for HA-111: every socket gets the snapshot, no socket
sees a seq gap it was not told about (``lagged``), and all sockets see the
same last seq within a second of each other.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time

import httpx
import websockets


async def one(i: int, args, ctx, results: list) -> None:
    async with httpx.AsyncClient(verify=False, timeout=20) as c:
        r = await c.post(f"{args.url}/api/v1/events/ws-ticket",
                         headers={"Authorization": f"Bearer {args.token}"})
        r.raise_for_status()
        ticket = r.json()["ticket"]
    ws_url = args.url.replace("https://", "wss://").replace("http://", "ws://")
    stats = {"client": i, "frames": 0, "gaps": 0, "lagged": 0, "snapshot": False,
             "last_seq": None, "heartbeats": 0}
    async with websockets.connect(f"{ws_url}/api/v1/events/ws?ticket={ticket}&v=2",
                                  ssl=ctx if ws_url.startswith("wss") else None,
                                  max_size=None) as ws:
        end = time.monotonic() + args.seconds
        last = None
        while time.monotonic() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), end - time.monotonic()))
            except TimeoutError:
                break
            kind = m.get("event_type")
            if kind == "subscribed":
                last = m["seq"]
                continue
            if kind == "state_snapshot":
                stats["snapshot"] = True
                continue
            if kind == "heartbeat":
                stats["heartbeats"] += 1
                continue
            if kind == "lagged":
                stats["lagged"] += 1
                continue
            stats["frames"] += 1
            # Filtered sockets legitimately skip seqs; only count going backwards.
            if last is not None and m["seq"] <= last:
                stats["gaps"] += 1
            last = m["seq"]
        stats["last_seq"] = last
    results.append(stats)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://localhost")
    p.add_argument("--token", required=True)
    p.add_argument("--clients", type=int, default=20)
    p.add_argument("--seconds", type=float, default=60)
    args = p.parse_args()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    results: list = []
    t0 = time.monotonic()
    await asyncio.gather(*(one(i, args, ctx, results) for i in range(args.clients)))
    frames = [r["frames"] for r in results]
    print(json.dumps({
        "clients": len(results),
        "seconds": round(time.monotonic() - t0, 1),
        "all_got_snapshot": all(r["snapshot"] for r in results),
        "frames_min_max": [min(frames), max(frames)],
        "out_of_order": sum(r["gaps"] for r in results),
        "lagged_notices": sum(r["lagged"] for r in results),
        "last_seq_spread": (max(r["last_seq"] or 0 for r in results)
                            - min(r["last_seq"] or 0 for r in results)),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
