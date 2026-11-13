# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`AsyncOpenNVR` — the same platform surface, awaited.

Demonstrates: `AsyncOpenNVR`, concurrent fan-out over the roster.

Same methods as `OpenNVR`, same scoping, same errors — use it inside an
app that is already async (the archetype run loops are), so a slow
snapshot on one camera does not block the others.
"""
import asyncio

from opennvr_app_sdk import AsyncOpenNVR


async def snapshot_everything() -> dict[str, int]:
    """Fetch a frame from every assigned camera at once, rather than
    one after another — on a 30-camera site this is the difference
    between 200ms and six seconds."""
    async with AsyncOpenNVR() as nvr:
        cameras = await nvr.cameras()
        frames = await asyncio.gather(
            *(nvr.snapshot(c) for c in cameras), return_exceptions=True)
        return {
            c.handle: len(f) if isinstance(f, bytes) else 0
            for c, f in zip(cameras, frames)
        }


async def main() -> None:
    print(await snapshot_everything())


if __name__ == "__main__":
    asyncio.run(main())
